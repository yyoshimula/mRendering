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
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import List, Optional, Tuple
import numpy as np

# プロジェクトルートを sys.path に追加（yoshimulib のため）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mitsuba as mi

from yoshimulib.attitude.quaternion import q2dcm
from relative_camera import chief_origin, resolve_camera
from asset_bootstrap import ensure_starfield_envmap

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


def scene_deputy_state(args, chief_q, rel_pos, rel_q):
    """Positions are Hill components by default; relative attitude retains its convention."""
    if args.mode == 'absolute':
        return rel_pos, rel_q
    pos, quat = compose_deputy_state(np.zeros(3), chief_q, rel_pos, rel_q)
    if getattr(args, 'relative_frame', 'hill') == 'hill':
        pos = np.asarray(rel_pos, dtype=float)
    return pos, quat


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
        extension = Path(model_path).suffix.lower()
        if extension in ('.glb', '.gltf'):
            from asset_cache import glb_to_obj_cached
            model_path = str(glb_to_obj_cached(Path(model_path)))
        elif extension == '.ply':
            objects[f'{prefix}_mesh'] = {
                'type': 'ply', 'filename': model_path,
                'to_world': body_transform @ mi.ScalarTransform4f.scale(scale),
                'bsdf': default_bsdf or {'type': 'diffuse', 'reflectance': .55},
            }
            return
        objects.update(load_model_with_parts(
            model_path, part_bsdfs or {}, default_bsdf or {},
            body_transform, scale, prefix=prefix,
        ))
    else:
        # プロシージャル衛星はキー名にプレフィックスを付けて両機の衝突を防ぐ
        for key, val in create_satellite(body_transform @ mi.ScalarTransform4f.scale(scale)).items():
            objects[f'{prefix}_{key}'] = val


# ---------------------------------------------------------------------------
# シーン構築
# ---------------------------------------------------------------------------
# 可視キャップクロップのメモ化: (src, mtime, 丸めた UV 範囲, 上限) → 一時 JPG。
# split_obj_by_parts と同じくプロセス内キャッシュ（フレームループ・ライブの
# 再レンダで切り出しを繰り返さないため）。
_EARTH_CROP_CACHE: dict = {}
_EARTH_CROP_LOCK = threading.Lock()


# --- NASA GIBS (WMTS, EPSG:4326) オンデマンド取得 -------------------------
# 実写の日次衛星画像（雲込み）を可視域ぶんだけダウンロードして地球テクスチャに
# 使う。TileMatrixSet '250m': level 8 = 320×160 タイル（512px 角、1.125°/タイル、
# 244 m/px 相当 163840×81920 px 全球）。level が 1 下がるごとに解像度 1/2。
# 出典表記: "Imagery provided by services from NASA's Global Imagery Browse
# Services (GIBS), part of NASA's Earth Science Data and Information System."
_GIBS_URL = ('https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/{layer}/'
             'default/{date}/250m/{z}/{row}/{col}.jpeg')
_GIBS_L8_W, _GIBS_L8_H, _GIBS_TILE = 163840, 81920, 512
_GIBS_CACHE_DIR = Path(__file__).resolve().parent / 'runs' / '_gibs_cache'


def _parse_gibs_src(src_path: str):
    """'gibs://<layer>/<date>/<z>' をパースして (layer, date, z) を返す。"""
    body = src_path[len('gibs://'):]
    layer, date, z = body.rsplit('/', 2)
    return layer, date, int(z)


def _read_gibs_region(src_path: str, r0: int, r1: int,
                      c0: int, c1: int) -> Optional[np.ndarray]:
    """GIBS からグローバル画素矩形 [r0:r1, c0:c1]（level z 座標）を読み出す。

    必要タイルだけ runs/_gibs_cache/ へダウンロード（並列・以後再利用）。
    列 c は wrap 可。個別タイルの取得失敗は黒で埋めるが、1 枚も取れなければ
    None（呼び出し側で BMNG 等へフォールバック）。
    """
    from concurrent.futures import ThreadPoolExecutor
    import urllib.request
    from PIL import Image

    layer, date, z = _parse_gibs_src(src_path)
    T = _GIBS_TILE
    n_cols = max(1, math.ceil((_GIBS_L8_W // (2 ** (8 - z))) / T))
    cache = _GIBS_CACHE_DIR / layer / date
    cache.mkdir(parents=True, exist_ok=True)

    tiles = [(tr, tc)
             for tr in range(r0 // T, (r1 - 1) // T + 1)
             for tc in range(c0 // T, (c1 - 1) // T + 1)]

    def fetch(t):
        tr, tc = t
        path = cache / f'z{z}_r{tr}_c{tc % n_cols}.jpeg'
        if path.exists() and path.stat().st_size > 0:
            return t, path
        url = _GIBS_URL.format(layer=layer, date=date, z=z,
                               row=tr, col=tc % n_cols)
        req = urllib.request.Request(url, headers={'User-Agent': 'mrender/1.0'})
        for _ in range(2):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                tmp = path.with_suffix('.part')
                tmp.write_bytes(data)
                tmp.rename(path)
                return t, path
            except Exception:  # noqa: BLE001 - リトライ後は欠損タイル扱い
                continue
        return t, None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = dict(pool.map(fetch, tiles))
    ok = sum(1 for p in results.values() if p is not None)
    if ok == 0:
        print(f'  gibs: {layer} {date} のタイルが 1 枚も取得できません'
              '（未来日・提供期間外・ネットワーク不通など）。'
              'ローカルの地球テクスチャへフォールバックします', file=sys.stderr)
        return None
    if ok < len(tiles):
        print(f'  gibs: {len(tiles) - ok}/{len(tiles)} タイル取得失敗（黒で充填）',
              file=sys.stderr)

    out = np.zeros((r1 - r0, c1 - c0, 3), dtype=np.uint8)
    for (tr, tc), path in results.items():
        gr0, gc0 = tr * T, tc * T
        rr0, rr1 = max(r0, gr0), min(r1, gr0 + T)
        cc0, cc1 = max(c0, gc0), min(c1, gc0 + T)
        if path is None:
            continue
        ta = np.asarray(Image.open(path).convert('RGB'))
        out[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] = \
            ta[rr0 - gr0:rr1 - gr0, cc0 - gc0:cc1 - gc0]
    return out


def _equirect_source_info(src_path: str):
    """equirect ソースの種別を判定して (kind, meta, mtime, (W, H)) を返す。

    kind:
      'file'  … 単一 equirect 画像（従来）
      'tiled' … meta.json を持つタイル分割ディレクトリ
                （tools/prepare_bmng.py が生成。全球を tile px 角の格子に分割）
      'gibs'  … 'gibs://<layer>/<date>/<z>' 形式の NASA GIBS オンデマンド取得
    判定不能なら None（呼び出し側でフォールバック）。
    """
    import json
    if isinstance(src_path, str) and src_path.startswith('gibs://'):
        try:
            layer, date, z = _parse_gibs_src(src_path)
        except ValueError:
            return None
        scale = 2 ** (8 - z)
        # mtime の代わりに日付+レイヤ+レベルがキャッシュキーを一意化する
        return ('gibs', None, 0.0, (_GIBS_L8_W // scale, _GIBS_L8_H // scale))
    p = Path(src_path)
    try:
        if p.is_dir():
            mp = p / 'meta.json'
            if not mp.exists():
                return None
            meta = json.loads(mp.read_text())
            return ('tiled', meta, os.path.getmtime(mp),
                    (int(meta['width']), int(meta['height'])))
        if p.exists():
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(p) as img:
                size = img.size
            return ('file', None, os.path.getmtime(p), size)
    except Exception:  # noqa: BLE001 - 読めなければ従来経路にフォールバック
        return None
    return None


def _read_tiled_region(dir_path: str, meta: dict,
                       r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
    """タイル分割 equirect ソースからグローバル画素矩形を読み出す。

    行 r は [0, height) 内前提。列 c は wrap 可（c0 < 0 / c1 > width）で、
    タイル列番号を剰余で引く。可視域にかかるサブタイルだけデコードするので
    全球 86400×43200 でもメモリ・時間は矩形サイズ相当で済む。
    """
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    T = int(meta['tile'])
    n_cols = int(meta['cols'])
    pattern = meta['pattern']
    out = np.empty((r1 - r0, c1 - c0, 3), dtype=np.uint8)
    tiles = [(tr, tc)
             for tr in range(r0 // T, (r1 - 1) // T + 1)
             for tc in range(c0 // T, (c1 - 1) // T + 1)]

    def read_one(t):
        tr, tc = t
        gr0, gc0 = tr * T, tc * T
        rr0, rr1 = max(r0, gr0), min(r1, gr0 + T)
        cc0, cc1 = max(c0, gc0), min(c1, gc0 + T)
        tile_path = Path(dir_path) / pattern.format(row=tr, col=tc % n_cols)
        ta = np.asarray(Image.open(tile_path).convert('RGB'))
        out[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] = \
            ta[rr0 - gr0:rr1 - gr0, cc0 - gc0:cc1 - gc0]

    # JPEG デコードは PIL の C 層で GIL を離すのでスレッド並列が効く
    # （absolute のクロップ再センタで数十枚を読むため支配的コストになる）
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(read_one, tiles))
    return out


def _crop_equirect_texture(src_path: str, u0: float, u1: float,
                           v0: float, v1: float, max_dim: int = 8192):
    """equirect テクスチャから UV 矩形を切り出して一時 PNG に保存する。

    u は経度方向（u0 < 0 / u1 > 1 で画像端をまたぐ wrap 指定可）、
    v は緯度方向 [0,1]（wrap なし）。切り出し範囲はピクセル境界へ丸める。
    ソースは単一 equirect 画像かタイル分割ディレクトリ（_equirect_source_info）。

    Returns:
        (crop_path, (u0p, u1p, v0p, v1p)) — 丸め後の実際の UV 範囲。
        読み込み失敗時は None。
    """
    info = _equirect_source_info(src_path)
    if info is None:
        return None
    kind, meta, mtime, (W, H) = info
    key = (str(src_path), mtime,
           round(u0, 6), round(u1, 6), round(v0, 6), round(v1, 6), int(max_dim))
    with _EARTH_CROP_LOCK:
        hit = _EARTH_CROP_CACHE.get(key)
    if hit is not None and os.path.exists(hit[0]):
        return hit

    try:
        from PIL import Image
    except ImportError:
        return None
    Image.MAX_IMAGE_PIXELS = None  # 21600x10800 等の decompression-bomb 警告を抑制
    r0 = max(0, int(math.floor(v0 * H)))
    r1 = min(H, max(r0 + 1, int(math.ceil(v1 * H))))
    c0 = int(math.floor(u0 * W))
    c1 = max(c0 + 1, int(math.ceil(u1 * W)))
    if c1 - c0 >= W:
        c0, c1 = 0, W
    try:
        if kind == 'gibs':
            arr = _read_gibs_region(src_path, r0, r1, c0, c1)
            if arr is None:
                return None
        elif kind == 'tiled':
            arr = _read_tiled_region(src_path, meta, r0, r1, c0, c1)
        else:
            img = Image.open(src_path).convert('RGB')
            arr = np.asarray(img)[r0:r1]
            if c0 < 0 or c1 > W:
                # 経度 wrap: 列インデックスを剰余で引く（連続画像として連結される）
                cols = np.arange(c0, c1) % W
                arr = arr[:, cols]
            else:
                arr = arr[:, c0:c1]
    except Exception:  # noqa: BLE001 - 欠損タイル等は従来経路へフォールバック
        return None

    crop = Image.fromarray(arr)
    if max(crop.size) > int(max_dim):
        s = float(max_dim) / max(crop.size)
        # 縮小専用なので面積平均（BOX）で十分（LANCZOS は 12000px 級で数秒かかり、
        # absolute のクロップ再センタ時のスパイクの主要因になる）
        crop = crop.resize((max(1, round(crop.size[0] * s)),
                            max(1, round(crop.size[1] * s))), Image.BOX)

    tmp_dir = tempfile.mkdtemp(prefix='mrender_earthcrop_')
    crop_path = str(Path(tmp_dir) / 'earth_crop.png')
    # ロスレスを保ったまま最速圧縮（既定 level 6 は 8192px 級で数秒かかる。
    # 一時ファイルなのでサイズより書き出し時間を優先する）
    crop.save(crop_path, compress_level=1)
    result = (crop_path, (c0 / W, c1 / W, r0 / H, r1 / H))
    with _EARTH_CROP_LOCK:
        _EARTH_CROP_CACHE[key] = result
    return result


_SPHERE_WARN_FILTER_INSTALLED = False


def _install_sphere_warn_filter() -> None:
    """回転付き地球 sphere の偽陽性警告だけを Mitsuba ロガーから落とす。

    Mitsuba sphere は to_world を絶対許容 1e-6 で decompose して shear /
    非等方スケールを警告するが、半径 6378 km スケールでは float32 の丸め
    （ULP ~5e-4）だけで必ず発火する（mi.rotate 製の純回転でも発火を実測）。
    該当 2 メッセージのみフィルタし、他の警告・進捗表示は素通しする。
    """
    global _SPHERE_WARN_FILTER_INSTALLED
    if _SPHERE_WARN_FILTER_INSTALLED:
        return

    class _FilteredAppender(mi.Appender):
        def __init__(self, inner):
            super().__init__()
            self._inner = inner

        def append(self, level, text):
            if "transform shouldn't contain" in text:
                return
            self._inner.append(level, text)

        def log_progress(self, progress, name, formatted, eta, ptr=None):
            self._inner.log_progress(progress, name, formatted, eta, ptr)

    try:
        logger = mi.Thread.thread().logger()
        inners = [logger.appender(i) for i in range(logger.appender_count())]
        logger.clear_appenders()
        for ap in inners:
            logger.add_appender(_FilteredAppender(ap))
        _SPHERE_WARN_FILTER_INSTALLED = True
    except Exception:
        # ロガー API が変わっても機能自体には影響させない（警告が出るだけ）
        _SPHERE_WARN_FILTER_INSTALLED = True


# UV 球メッシュの一時 OBJ（(nu, nv) → パス）。プロセス内キャッシュ
_UV_SPHERE_CACHE: dict = {}
_UV_SPHERE_LOCK = threading.Lock()


def _uv_sphere_obj(nu: int = 512, nv: int = 256) -> str:
    """Mitsuba の解析 sphere と同一 UV 規約の単位 UV 球 OBJ を生成して返す。

    mode=absolute の地球用。解析 sphere は to_world がスカラー状態のため、
    毎フレームの姿勢更新が Dr.Jit カーネルへのリテラル焼き込み→全再コンパイル
    （実測 ~2.7 s/frame, cuda）を誘発する。メッシュなら頂点/法線の配列更新で
    済み再コンパイルは起きない（PersistentScene の mesh_xform 経路に乗る）。

    UV 規約（実測確定済みの解析 sphere と同一）: u = atan2(y,x)/2π（+x で 0、
    経度シームは u=0/1 に複製列）、v = acos(z)/π（+z 極で 0）。クロップの
    to_uv 数学はそのまま適用できる。法線 = 頂点位置（単位球）。
    テッセレーション誤差: nu=512 で弦の落差 ~0.12 km、LEO の地平線距離からの
    見込み角 ~1e-5 rad と 1 画素（~1e-3 rad）より十分小さい。
    """
    key = (int(nu), int(nv))
    with _UV_SPHERE_LOCK:
        hit = _UV_SPHERE_CACHE.get(key)
    if hit is not None and os.path.exists(hit):
        return hit
    lines = ['# unit UV sphere (Mitsuba sphere UV convention)\n']
    for i in range(nv + 1):          # 緯線リング: v = i/nv, θ = vπ（+z 極から）
        theta = math.pi * i / nv
        st, ct = math.sin(theta), math.cos(theta)
        for j in range(nu + 1):      # 経線: u = j/nu, φ = 2πu
            phi = 2.0 * math.pi * j / nu
            x, y, z = st * math.cos(phi), st * math.sin(phi), ct
            lines.append(f'v {x:.8f} {y:.8f} {z:.8f}\n')
            lines.append(f'vn {x:.8f} {y:.8f} {z:.8f}\n')
            lines.append(f'vt {j / nu:.8f} {1.0 - i / nv:.8f}\n')
    # OBJ の vt は下原点なので v を反転して書き、参照時に Mitsuba が再反転する
    def vid(i, j):
        return i * (nu + 1) + j + 1
    for i in range(nv):
        for j in range(nu):
            a, b = vid(i, j), vid(i, j + 1)
            c, d = vid(i + 1, j + 1), vid(i + 1, j)
            if i > 0:            # 上極の縮退三角形はスキップ
                lines.append(f'f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}\n')
            if i < nv - 1:       # 下極の縮退三角形はスキップ
                lines.append(f'f {a}/{a}/{a} {c}/{c}/{c} {d}/{d}/{d}\n')
    tmp_dir = tempfile.mkdtemp(prefix='mrender_uvsphere_')
    path = str(Path(tmp_dir) / f'uv_sphere_{nu}x{nv}.obj')
    with open(path, 'w') as f:
        f.writelines(lines)
    with _UV_SPHERE_LOCK:
        _UV_SPHERE_CACHE[key] = path
    return path


def create_earth_backdrop(chief_pos, direction, altitude_km: float,
                          texture: str, rotation_deg: float = 0.0,
                          *, highres_texture: Optional[str] = None,
                          crop: bool = True,
                          crop_margin_deg: float = 5.0,
                          crop_max_dim: int = 8192,
                          gibs: bool = False,
                          gibs_layer: str = 'MODIS_Terra_CorrectedReflectance_TrueColor',
                          gibs_date: Optional[str] = None,
                          orientation: Optional[np.ndarray] = None,
                          analytic_sphere: bool = False) -> dict:
    """chief 近傍シーン（km 単位）に地球球体を背景として置く。

    シーン座標は chief 中心の RTN(Hill) frame 想定なので、`direction` に
    [0,0,-1]（-N 方向 = 地心方向）などを与えると chief の高度 `altitude_km`
    に対応する位置へ地球（半径 R_EARTH_KM の解析球）が置かれる。
    パストレーシングにより地球アルベド（earthshine）の照り返しも自動で乗る。

    可視域クロップ（crop=True、既定）:
      LEO では地平線までの可視キャップ（中心角 γ = acos(R/(R+h))、400 km で
      ~20°）しか見えないのに全球テクスチャを貼ると解像度の大半が無駄になる。
      そこで chief 直下点まわりのキャップ + 余白だけを高解像度ソース
      （highres_texture、無ければ texture）から切り出し、球を余分に自転させて
      キャップ中心を u=0.5 に寄せた上で bitmap の to_uv でクロップ範囲へ
      再マップする（見た目の地理は不変、実効解像度だけ上がる。
      Mitsuba sphere の UV 規約 u=atan2(y,x)/2π・v=acos(z)/π は実測確認済み）。
      キャップが極を含む場合は経度全周を切り出す。クロップ不能時
      （PIL 無し・ファイル無し等）は従来どおり texture を全球に貼る。

    orientation（mode=absolute 用）:
      3×3 の ECEF→シーン回転行列を与えると、z 軸自転（rotation_deg）の代わりに
      フル 3 軸の地球姿勢で球を置く。rotation_deg は無視され、直下点は
      orientationᵀ·(chief 方向) から任意の緯度経度になる。
      クロップの数学は共通（cl の求め方と to_world の回転だけ差し替え）。
      テクスチャ規約: equirect 地球画像は u=0 が経度 −180°（日付変更線）、
      Mitsuba sphere は u=atan2(y,x)/2π（local +x 方向で u=0）なので、
      ECEF（+x = 経度 0°）とテクスチャ local frame の間に Rz(180°) を挟む
      （でないと経度が 180° ずれた地表が貼られる。実測で確認済み）。
    """
    from yoshimulib.orbit.orbital_elements import R_EARTH_KM
    d = np.asarray(direction, dtype=float)
    d = d / np.linalg.norm(d)
    center = np.asarray(chief_pos, dtype=float) + d * (R_EARTH_KM + altitude_km)

    if orientation is not None:
        # ECEF→テクスチャ local の経度 180° オフセット（Rz(180) = diag(-1,-1,1)）
        orientation = np.asarray(orientation, dtype=float) @ np.diag([-1.0, -1.0, 1.0])

    # orientation 指定時は rot_deg を「local z の追加回転（クロップ再センタ用）」
    # として 0 から積む。未指定時は従来どおりユーザ指定の自転角
    rot_deg = 0.0 if orientation is not None else float(rotation_deg)
    reflectance = {'type': 'bitmap', 'filename': texture}

    if crop:
        # chief 直下点（キャップ中心）を元の local frame（rotation_deg 適用後）で表す
        c = -d  # 地心 → chief 方向
        if orientation is not None:
            cl = np.asarray(orientation, dtype=float).T @ c
        else:
            rot = math.radians(rot_deg)
            cl = np.array([c[0] * math.cos(rot) + c[1] * math.sin(rot),
                           -c[0] * math.sin(rot) + c[1] * math.cos(rot),
                           c[2]])
        theta_c = math.acos(max(-1.0, min(1.0, cl[2])))
        u_c_raw = (math.atan2(cl[1], cl[0]) / (2.0 * math.pi)) % 1.0
        gamma = (math.acos(R_EARTH_KM / (R_EARTH_KM + max(altitude_km, 1e-6)))
                 + math.radians(max(crop_margin_deg, 0.0)))
        if orientation is not None:
            # mode=absolute では直下点が毎フレーム動き、クロップ矩形を実数のまま
            # 使うと _crop_equirect_texture のキャッシュが一切当たらない
            # （タイルデコードで数 s/frame）。直下点を qstep グリッドに量子化し
            # 余白を qstep 広げれば、キャップは常に量子化窓に収まったまま
            # 隣接フレームが同一クロップを共有できる（to_uv は厳密再マップ
            # なので見た目の地理は不変。実効解像度が余白分だけ僅かに下がるのみ）
            qstep = math.radians(max(2.0, crop_margin_deg))
            theta_c = min(math.pi, max(0.0, round(theta_c / qstep) * qstep))
            u_step = qstep / (2.0 * math.pi)
            u_c_raw = (round(u_c_raw / u_step) * u_step) % 1.0
            # gamma は高度依存（楕円軌道では毎フレーム変動）なので、量子化余白を
            # 足した上で qstep グリッドへ切り上げる。でないと矩形が毎フレーム
            # 微妙にズレてキャッシュが当たらない
            gamma = math.ceil((gamma + qstep) / qstep) * qstep
        v0 = max(0.0, (theta_c - gamma) / math.pi)
        v1 = min(1.0, (theta_c + gamma) / math.pi)
        pole_in_cap = (theta_c - gamma <= 0.0) or (theta_c + gamma >= math.pi)
        if pole_in_cap:
            u_lo, u_hi, delta_u = 0.0, 1.0, 0.0
        else:
            # キャップの経度半幅（球面キャップの bounding longitude）
            dlam = math.asin(min(1.0, math.sin(gamma) / math.sin(theta_c)))
            du = dlam / math.pi  # = 2*dlam / (2π)
            u_c = u_c_raw
            # 球を余分に δ 回してキャップ中心を u'=0.5 へ（wrap 回避）。
            # 新 u' でのテクスチャ列は元の u = u' + δ/360 に対応する。
            delta_u = u_c - 0.5
            u_lo, u_hi = 0.5 - du, 0.5 + du

        # ソース候補: GIBS（指定時）→ highres（単一画像 or タイル分割 dir）
        # → 21600px 単一画像 → 表示用 texture の順でフォールバック
        candidates: List[str] = []
        if gibs:
            if not gibs_date:
                from datetime import datetime, timedelta, timezone
                gibs_date = (datetime.now(timezone.utc)
                             - timedelta(days=1)).date().isoformat()
            # 可視域の必要画素数からズームレベルを選ぶ（crop_max_dim を大きく
            # 超える level 8 を無駄に落とさない）
            need = max((u_hi - u_lo) * _GIBS_L8_W, (v1 - v0) * _GIBS_L8_H)
            z = 8
            while z > 0 and need / (2 ** (8 - z)) > crop_max_dim * 1.25:
                z -= 1
            candidates.append(f'gibs://{gibs_layer}/{gibs_date}/{z}')
        for cand in (highres_texture, 'assets/textures/earth_day.jpg', texture):
            if cand:
                candidates.append(str(cand))

        if (v1 - v0) < 0.95 or (u_hi - u_lo) < 0.95:
            for src in candidates:
                if _equirect_source_info(src) is None:
                    continue
                res = _crop_equirect_texture(
                    src, u_lo + delta_u, u_hi + delta_u, v0, v1, crop_max_dim)
                if res is None:
                    continue  # 取得失敗（ネットワーク等）→ 次候補へ
                crop_path, (cu0, cu1, cv0, cv1) = res
                # ピクセル丸め後の範囲を u' 空間へ戻して to_uv を組む
                u0p, u1p = cu0 - delta_u, cu1 - delta_u
                rot_deg = rot_deg + delta_u * 360.0
                to_uv = (mi.ScalarTransform4f.scale(
                             [1.0 / (u1p - u0p), 1.0 / (cv1 - cv0), 1.0])
                         @ mi.ScalarTransform4f.translate([-u0p, -cv0, 0.0]))
                reflectance = {'type': 'bitmap', 'filename': crop_path,
                               'wrap_mode': 'clamp', 'to_uv': to_uv}
                break

    if orientation is not None:
        rot4 = np.eye(4)
        rot4[:3, :3] = np.asarray(orientation, dtype=float)
        if analytic_sphere:
            # 従来経路（比較・検証用 --earth-analytic-sphere）。
            # 注意: 地球サイズ（半径 6378 km）の sphere に回転を与えると、
            # Mitsuba の decompose チェック（絶対許容 1e-6）が float32 の丸め
            # （ULP ~5e-4）だけで shear/非等方スケールの偽陽性警告を毎回出す。
            # 実測で mi.rotate 製の純回転でも出るため実害なし。ログ汚染を防ぐ
            # ため該当メッセージのみ _install_sphere_warn_filter() で抑制する。
            # また解析 sphere の to_world はスカラー状態なので、毎フレーム更新は
            # Dr.Jit カーネル再コンパイル（cuda 実測 ~2.7 s/frame）を誘発する
            _install_sphere_warn_filter()
            to_world = (mi.ScalarTransform4f.translate(center.tolist())
                        @ mi.ScalarTransform4f(rot4.tolist())
                        @ mi.ScalarTransform4f.rotate([0, 0, 1], rot_deg))
            earth = {
                'type': 'sphere',
                'radius': float(R_EARTH_KM),
                'to_world': to_world,
            }
        else:
            # 既定: UV 球メッシュ（_uv_sphere_obj）。毎フレームの姿勢更新が
            # PersistentScene の頂点配列更新（mesh_xform）に乗り、
            # カーネル再コンパイルが起きない
            to_world = (mi.ScalarTransform4f.translate(center.tolist())
                        @ mi.ScalarTransform4f(rot4.tolist())
                        @ mi.ScalarTransform4f.rotate([0, 0, 1], rot_deg)
                        @ mi.ScalarTransform4f.scale([float(R_EARTH_KM)] * 3))
            earth = {
                'type': 'obj',
                'filename': _uv_sphere_obj(),
                'to_world': to_world,
            }
    else:
        to_world = (mi.ScalarTransform4f.translate(center.tolist())
                    @ mi.ScalarTransform4f.rotate([0, 0, 1], rot_deg)
                    @ mi.ScalarTransform4f.scale([R_EARTH_KM] * 3))
        earth = {
            'type': 'sphere',
            'to_world': to_world,
        }
    earth['bsdf'] = {
        'type': 'diffuse',
        'reflectance': reflectance,
    }
    return {'earth': earth}


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
                hide_chief: bool = False, camera_mode: str = 'manual',
                camera_offset=(0., 0., 0.), camera_direction=(1., 0., 0.),
                camera_body_up=(0., 0., 1.)) -> dict:
    """Mitsuba シーン辞書を組み立てる。"""
    camera = resolve_camera(camera_mode, np.array(chief_xform.matrix), np.array(deputy_xform.matrix),
                            origin=camera_origin, target=camera_target, up=camera_up,
                            offset=camera_offset, direction=camera_direction, body_up=camera_body_up)
    camera_origin, camera_target, camera_up = camera['origin'], camera['target'], camera['up']
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

    if (not hide_chief or camera_mode.startswith('deputy_')) and not camera_mode.startswith('chief_'):
        add_object(scene_dict, chief_xform,
                   model_path=chief_model, part_bsdfs=chief_parts,
                   default_bsdf=chief_default_bsdf, scale=chief_scale, prefix='chief')
    if not camera_mode.startswith('deputy_'):
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
        # 既定の assets/starfield.exr は Git 管理外なので、無ければここで生成する。
        ensure_starfield_envmap(starfield)
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
            highres_texture=getattr(args, 'earth_highres_texture', None),
            crop=bool(getattr(args, 'earth_crop', True)),
            crop_margin_deg=float(getattr(args, 'earth_crop_margin_deg', 5.0)),
            crop_max_dim=int(getattr(args, 'earth_crop_max_dim', 8192)),
            gibs=bool(getattr(args, 'earth_gibs', False)),
            gibs_layer=str(getattr(args, 'earth_gibs_layer', None)
                           or 'MODIS_Terra_CorrectedReflectance_TrueColor'),
            gibs_date=getattr(args, 'earth_gibs_date', None),
        )
    return sun_rgb, env_dict, earth_dict


# ---------------------------------------------------------------------------
# absolute モード: 絶対軌道 2 本 → 相対状態 + 時変環境
# ---------------------------------------------------------------------------
class AbsoluteOrbitContext:
    """mode=absolute の軌道・環境コンテキスト。

    chief/deputy の絶対軌道（ケプラー要素 [deg 入力] or CsvEphemeris [rad]）を
    保持し、時刻 t [s]（epoch_utc 起点）の相対状態と時変環境を返す。
    バッチ (_run) と live_worker の両方がこのクラスを使い、画作りロジックを
    1 箇所に閉じ込める（他 verb と同じ「実装は relative_motion 側」方式）。

    座標系: シーン = chief 中心 RTN(LVLH) frame [km]（x=R, y=T, z=N）。
      - A_i2rtn(t): compute_lvlh_frame の基底 (R,S,W) を行に並べた ECI→RTN DCM
      - rel_pos = A_i2rtn · (r_d − r_c)
      - 太陽方向（シーン）= A_i2rtn · unit(sun_eci − r_c)（VSOP87、epoch_utc 基準）
      - 食: yoshimulib shadow() の照射率 ν ∈ [0,1] を太陽照度に乗算
      - 地球姿勢: ECEF→シーン = A_i2rtn · Rz(GMST)。直下点が地上軌跡どおり動き、
        create_earth_backdrop(orientation=...) が毎フレーム可視域をクロップする
      - 星空 envmap: to_world = A_i2rtn（恒星は ECI 固定 = シーンの回転に伴い流れる）
    """

    def __init__(self, args: argparse.Namespace) -> None:
        from ground_observation import parse_epoch_jd
        self.jd0 = float(parse_epoch_jd(str(args.epoch_utc)))
        self._chief_eph, self._chief_el = self._make_orbit(
            getattr(args, 'chief_orbit_csv', None), args.chief_oe, 'chief')
        self._deputy_eph, self._deputy_el = self._make_orbit(
            getattr(args, 'deputy_orbit_csv', None), args.deputy_oe, 'deputy')
        self.deputy_attitude = str(getattr(args, 'deputy_attitude', 'tumble'))
        if self.deputy_attitude == 'csv' and self._deputy_eph is None:
            raise ValueError('--deputy-attitude csv には --deputy-orbit-csv が必要です')

    @staticmethod
    def _make_orbit(csv_path: Optional[str], oe_deg: Optional[list], label: str):
        """(CsvEphemeris or None, OrbitalElements or None) を返す。CSV 優先。"""
        from orbit_mechanics import CsvEphemeris, OrbitalElements
        if csv_path:
            return CsvEphemeris(str(csv_path)), None
        if oe_deg is None:
            raise ValueError(
                f'mode=absolute では {label} の軌道が必要です'
                f'（--{label}-oe か --{label}-orbit-csv）')
        a, e, inc, raan, argp, m0 = [float(v) for v in oe_deg]
        el = OrbitalElements(
            semi_major_axis=a, eccentricity=e,
            inclination=math.radians(inc), raan=math.radians(raan),
            arg_periapsis=math.radians(argp), mean_anomaly_0=math.radians(m0))
        return None, el

    @staticmethod
    def _state(eph, el, t: float) -> Tuple[np.ndarray, np.ndarray]:
        from orbit_mechanics import compute_orbital_position
        if eph is not None:
            r, v = eph.state_at(float(t))
        else:
            r, v = compute_orbital_position(el, float(t))
        return np.asarray(r, dtype=float), np.asarray(v, dtype=float)

    def chief_state(self, t: float) -> Tuple[np.ndarray, np.ndarray]:
        return self._state(self._chief_eph, self._chief_el, t)

    def deputy_state(self, t: float) -> Tuple[np.ndarray, np.ndarray]:
        return self._state(self._deputy_eph, self._deputy_el, t)

    @staticmethod
    def rtn_dcm(r: np.ndarray, v: np.ndarray) -> np.ndarray:
        """ECI→RTN の DCM（行 = R, S, W 基底）。"""
        from orbit_mechanics import compute_lvlh_frame
        r_vec, s_vec, w_vec = compute_lvlh_frame(r, v)
        return np.vstack([r_vec, s_vec, w_vec])

    def relative_state(self, t: float
                       ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(rel_pos_rtn [km], A_i2rtn, r_chief_eci, v_chief_eci) を返す。"""
        r_c, v_c = self.chief_state(t)
        r_d, _v_d = self.deputy_state(t)
        a_i2rtn = self.rtn_dcm(r_c, v_c)
        rel_pos = a_i2rtn @ (r_d - r_c)
        return rel_pos, a_i2rtn, r_c, v_c

    def initial_tumble_q_eci(self, t0: float, rel_q_init: np.ndarray) -> np.ndarray:
        """tumble の初期 ECI→Body クォータニオン。

        rel_quat は「t=t0 の chief RTN に対する deputy 初期姿勢」と解釈:
        A_eci2body(t0) = A(rel_q_init) · A_i2rtn(t0)
        """
        from yoshimulib.attitude.quaternion import dcm2q
        _, a_i2rtn, _, _ = self.relative_state(t0)
        a_eci2body = q2dcm(np.asarray(rel_q_init, dtype=float), scalar=SCALAR) @ a_i2rtn
        q = dcm2q(a_eci2body, scalar=SCALAR)
        return q / np.linalg.norm(q)

    def deputy_attitude_dcm(self, t: float, a_i2rtn: np.ndarray,
                            rel_q_init: np.ndarray,
                            q_dep_eci: Optional[np.ndarray]) -> np.ndarray:
        """時刻 t の deputy の ECI→Body DCM を姿勢モードに応じて返す。

        tumble は呼び出し側が propagate_attitude で進める q_dep_eci（ECI 系
        トルクフリー伝播）を渡す。lvlh は deputy 自身の RTN に rel_quat を
        固定オフセット。csv は deputy 軌道 CSV の q1..q4（ECI→Body）。
        """
        if self.deputy_attitude == 'tumble':
            if q_dep_eci is None:
                raise ValueError('tumble には q_dep_eci が必要です')
            return q2dcm(np.asarray(q_dep_eci, dtype=float), scalar=SCALAR)
        if self.deputy_attitude == 'lvlh':
            r_d, v_d = self.deputy_state(t)
            a_dep_rtn = self.rtn_dcm(r_d, v_d)
            return q2dcm(np.asarray(rel_q_init, dtype=float), scalar=SCALAR) @ a_dep_rtn
        # csv
        body_to_eci = self._deputy_eph.attitude_at(float(t))
        if body_to_eci is None:
            raise ValueError(
                f'--deputy-orbit-csv に姿勢列 q1..q4 がありません: '
                f'{getattr(self._deputy_eph, "csv_path", "")}')
        # CsvEphemeris はクォータニオンではなく Body→ECI の DCM を返す。
        return np.asarray(body_to_eci, dtype=float).T

    def rel_quat_scene(self, a_eci2body: np.ndarray, a_i2rtn: np.ndarray
                       ) -> np.ndarray:
        """ECI→Body DCM をシーン(RTN)→Body クォータニオンに落とす。"""
        from yoshimulib.attitude.quaternion import dcm2q
        q = dcm2q(a_eci2body @ a_i2rtn.T, scalar=SCALAR)
        return q / np.linalg.norm(q)

    def environment_at(self, t: float, r_c: np.ndarray, a_i2rtn: np.ndarray,
                       args: argparse.Namespace
                       ) -> Tuple[float, np.ndarray, np.ndarray, Optional[dict]]:
        """時刻 t の (ν, sun_dir_scene, ecef2scene, earth_dict) を返す。

        ν は本影/半影の照射率（太陽 RGB に乗算）、sun_dir_scene は
        シーン座標での「太陽の方向」単位ベクトル（Mitsuba directional の
        direction には −sun_dir_scene を渡す）。
        """
        from ground_observation import sun_position_eci_km, SUN_RADIUS_KM
        from orbit_mechanics import EARTH_RADIUS_KM
        from yoshimulib.orbit.sidereal import gmst
        from yoshimulib.orbit.transforms import shadow as earth_shadow

        jd = self.jd0 + float(t) / 86400.0
        sun_eci = np.asarray(sun_position_eci_km(jd), dtype=float).reshape(3)
        nu = float(np.atleast_1d(earth_shadow(
            r_c.reshape(1, 3), sun_eci.reshape(1, 3),
            SUN_RADIUS_KM, EARTH_RADIUS_KM))[0])
        sun_dir = sun_eci - r_c
        sun_dir_scene = a_i2rtn @ (sun_dir / np.linalg.norm(sun_dir))

        theta = float(np.atleast_1d(gmst(jd))[0])
        ct, st = math.cos(theta), math.sin(theta)
        rz = np.array([[ct, -st, 0.0], [st, ct, 0.0], [0.0, 0.0, 1.0]])
        ecef2scene = a_i2rtn @ rz  # v_scene = A_i2rtn · Rz(GMST) · v_ecef

        earth_dict = None
        if args.show_earth:
            alt_km = float(np.linalg.norm(r_c)) - EARTH_RADIUS_KM
            earth_dict = create_earth_backdrop(
                [0.0, 0.0, 0.0], [-1.0, 0.0, 0.0], alt_km,
                str(args.earth_texture),
                highres_texture=getattr(args, 'earth_highres_texture', None),
                crop=bool(getattr(args, 'earth_crop', True)),
                crop_margin_deg=float(getattr(args, 'earth_crop_margin_deg', 5.0)),
                crop_max_dim=int(getattr(args, 'earth_crop_max_dim', 8192)),
                gibs=bool(getattr(args, 'earth_gibs', False)),
                gibs_layer=str(getattr(args, 'earth_gibs_layer', None)
                               or 'MODIS_Terra_CorrectedReflectance_TrueColor'),
                gibs_date=getattr(args, 'earth_gibs_date', None),
                orientation=ecef2scene,
                analytic_sphere=bool(getattr(args, 'earth_analytic_sphere', False)),
            )
        return nu, sun_dir_scene, ecef2scene, earth_dict

    @staticmethod
    def rotate_env_dict(env_dict: Optional[dict], a_i2rtn: np.ndarray
                        ) -> Optional[dict]:
        """starfield envmap を ECI 固定にする（to_world = A_i2rtn の回転）。

        シーン(RTN)は慣性空間内で回転するので、envmap をシーンに固定すると
        恒星が chief と一緒に回る非物理になる。to_world に ECI→シーンの回転を
        与えれば恒星方向は慣性固定になる。constant 環境光はそのまま返す。
        """
        if not env_dict or env_dict.get('type') != 'envmap':
            return env_dict
        rot4 = np.eye(4)
        rot4[:3, :3] = np.asarray(a_i2rtn, dtype=float)
        out = dict(env_dict)
        out['to_world'] = mi.ScalarTransform4f(rot4.tolist())
        return out


# ---------------------------------------------------------------------------
# 永続シーン: mi.load_dict を毎フレームやり直さない高速化パス
# ---------------------------------------------------------------------------
_MESH_TYPES = ('obj', 'ply', 'serialized', 'cube')
_XFORM_TYPES = ('sphere', 'cylinder', 'disk', 'rectangle')


def _leaf_equal(a, b) -> bool:
    """シーン辞書の葉値の等価判定（Transform は行列で比較）。"""
    if hasattr(a, 'matrix') and hasattr(b, 'matrix'):
        return bool(np.array_equal(np.array(a.matrix), np.array(b.matrix)))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        try:
            return bool(np.array_equal(np.asarray(a, dtype=float),
                                       np.asarray(b, dtype=float)))
        except (TypeError, ValueError):
            return list(a) == list(b)
    return a == b


class PersistentScene:
    """シーンを 1 回だけ mi.load_dict し、以後は mi.traverse で差分更新する。

    毎フレームの `mi.load_dict` はシーンパース・BVH 構築・Dr.Jit カーネル
    再トレースで CPU 数百 ms〜数 s を食い、GPU バリアントではレンダ本体より
    支配的になる（GPU 使用率が数 % に張り付く原因）。このクラスは前フレームの
    scene_dict との差分を取り、更新可能なパラメータだけを書き換えて再レンダする。

    更新可能（traverse キーの存在は実測確認済み・Mitsuba 3.9）:
      - メッシュ (obj/ply/cube) の to_world      → 頂点/法線の剛体変換焼き直し
      - 解析形状 (sphere/cylinder 等) の to_world → <key>.to_world
      - directional の direction / irradiance    → sun の to_world / irradiance.value
        （direction は to_world の +z 列。基底の残り 2 列は放射に影響しない）
      - envmap の to_world / scale               → 星空の ECI 固定回転
    それ以外の差分（キー集合の変化・BSDF 変更・クロップテクスチャの
    ファイル差し替え等）は構造変化とみなし、従来どおり作り直す
    （absolute モードのクロップ再センタは量子化により十数フレームに 1 回
    なので、作り直しコストは償却される）。

    数値注意: 頂点は「初期ロード時に to_world を焼き込んだ値」へ相対変換
    M = T·T₀⁻¹ を掛けるため、毎フレーム作り直しと比べ float32 の丸め経路が
    変わり、出力は ±数 LSB 揺らぎうる（統計的品質は同一。ビット一致が
    必要なら --no-persistent-scene）。
    """

    def __init__(self) -> None:
        self.scene = None
        self.params = None
        self.prev: Optional[dict] = None
        self.base: dict = {}      # mesh key -> (T0_inv 4x4, v0 (N,3), n0 (N,3)|None)
        self.rebuilds = 0
        self.updates = 0

    # --- 構築 --------------------------------------------------------------
    def _build(self, scene_dict: dict) -> None:
        from asset_cache import preload_bitmaps, share_bsdf_textures
        self.scene = mi.load_dict(preload_bitmaps(share_bsdf_textures(scene_dict)))
        self.params = mi.traverse(self.scene)
        self.base = {}
        for key, node in scene_dict.items():
            if not (isinstance(node, dict) and node.get('type') in _MESH_TYPES):
                continue
            pk = f'{key}.vertex_positions'
            if pk not in self.params:
                continue
            v0 = np.array(self.params[pk], dtype=np.float64).reshape(-1, 3)
            nk = f'{key}.vertex_normals'
            n0 = (np.array(self.params[nk], dtype=np.float64).reshape(-1, 3)
                  if nk in self.params else None)
            t0 = (np.array(node['to_world'].matrix, dtype=np.float64)
                  if 'to_world' in node else np.eye(4))
            self.base[key] = (np.linalg.inv(t0), v0, n0)
        self.rebuilds += 1

    # --- 差分検出 ----------------------------------------------------------
    def _diff(self, prev: dict, cur: dict, path=()) -> Optional[list]:
        """葉の差分パスを列挙。構造差（キー集合/型の変化）は None を返す。"""
        if isinstance(prev, dict) and isinstance(cur, dict):
            if set(prev.keys()) != set(cur.keys()):
                return None
            out: list = []
            for k in cur:
                sub = self._diff(prev[k], cur[k], path + (k,))
                if sub is None:
                    return None
                out.extend(sub)
            return out
        if isinstance(prev, dict) != isinstance(cur, dict):
            return None
        return [] if _leaf_equal(prev, cur) else [path]

    def _classify(self, path: tuple, cur: dict):
        """差分パス → 更新アクション。未対応なら None（= 作り直し）。"""
        if len(path) < 2:
            return None
        top = path[0]
        node = cur.get(top)
        if not isinstance(node, dict):
            return None
        ntype = node.get('type')
        rest = path[1:]
        if rest == ('to_world',):
            if ntype in _MESH_TYPES:
                return ('mesh_xform', top)
            if ntype in _XFORM_TYPES or ntype == 'envmap':
                return ('to_world', top)
        if ntype == 'directional':
            if rest == ('direction',):
                return ('sun_dir', top)
            if rest == ('irradiance', 'value'):
                return ('sun_irr', top)
        if ntype == 'envmap' and rest == ('scale',):
            return ('env_scale', top)
        if rest[-1] in ('filename', 'to_uv') and len(rest) >= 2:
            # bitmap テクスチャの差し替え（absolute の可視域クロップ再センタ等）
            sub = node
            for k in rest[:-1]:
                sub = sub.get(k) if isinstance(sub, dict) else None
                if sub is None:
                    return None
            if isinstance(sub, dict) and sub.get('type') == 'bitmap':
                return ('bitmap', (top,) + tuple(rest[:-1]))
        return None

    # --- 更新適用 ----------------------------------------------------------
    def _apply(self, actions: set, cur: dict) -> None:
        # 法線は 2 段目で適用する: Mesh は vertex_positions が dirty になると
        # parameters_changed() で法線を幾何から自動再計算し、同一 update 内の
        # 法線代入を上書きしてしまう（OBJ 由来のハードエッジ法線が壊れ、
        # 作り直しパスと稜線のシェーディングがズレる）。位置だけ update した後、
        # 法線のみの update をもう 1 回かければ再計算は走らない。
        deferred_normals = []
        for kind, top in actions:
            node = cur[top] if not isinstance(top, tuple) else None
            if kind == 'mesh_xform':
                t0_inv, v0, n0 = self.base[top]
                m = np.array(node['to_world'].matrix, dtype=np.float64) @ t0_inv
                r = m[:3, :3]
                pos = v0 @ r.T + m[:3, 3]
                self.params[f'{top}.vertex_positions'] = mi.Float(
                    np.ascontiguousarray(pos, dtype=np.float32).ravel())
                if n0 is not None:
                    s = float(np.cbrt(abs(np.linalg.det(r)))) or 1.0
                    rn = r / s
                    nrm = n0 @ rn.T
                    deferred_normals.append(
                        (f'{top}.vertex_normals',
                         np.ascontiguousarray(nrm, dtype=np.float32).ravel()))
            elif kind == 'to_world':
                tw = node['to_world']
                if node.get('type') == 'sphere' and 'radius' in node:
                    # 解析 sphere は明示 radius を to_world に焼き込んで保持する
                    # （traverse の to_world = 辞書 to_world @ scale(radius)）
                    tw = tw @ mi.ScalarTransform4f.scale([float(node['radius'])] * 3)
                self.params[f'{top}.to_world'] = tw
            elif kind == 'sun_dir':
                d = np.asarray(node['direction'], dtype=float)
                d = d / np.linalg.norm(d)
                # directional の to_world は「local +z → 進行方向」のフレーム。
                # 残り 2 列は観測量に影響しないので任意の直交基底でよい
                a = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 \
                    else np.array([1.0, 0.0, 0.0])
                s = np.cross(a, d); s /= np.linalg.norm(s)
                t = np.cross(d, s)
                m = np.eye(4)
                m[:3, 0], m[:3, 1], m[:3, 2] = s, t, d
                self.params[f'{top}.to_world'] = mi.ScalarTransform4f(m.tolist())
            elif kind == 'sun_irr':
                self.params[f'{top}.irradiance.value'] = list(
                    node['irradiance']['value'])
            elif kind == 'env_scale':
                self.params[f'{top}.scale'] = float(node['scale'])
            elif kind == 'bitmap':
                # top はテクスチャ dict へのパス tuple（例: ('earth','bsdf','reflectance')）
                tex = cur
                for k in top:
                    tex = tex[k]
                prefix = '.'.join(top)
                # データの内部表現はバリアント依存（llvm はリニア Float32、
                # cuda はハードウェアテクスチャ向けの生値を保持し sRGB 変換を
                # サンプル時に行う）。自前変換は危険なので、使い捨ての bitmap
                # プラグインに新ファイルを読ませて変換済みテンソルを写す
                donor = mi.load_dict({'type': 'bitmap',
                                      'filename': str(tex['filename']),
                                      'wrap_mode': tex.get('wrap_mode', 'repeat')})
                self.params[f'{prefix}.data'] = mi.traverse(donor)['data']
                if 'to_uv' in tex:
                    # traverse 上の to_uv は UV 空間の 3x3（AffineTransform3f）。
                    # 辞書で渡す 4x4 から 2D アフィン部分を落として変換する
                    m4 = np.array(tex['to_uv'].matrix, dtype=np.float64)
                    m3 = np.array([[m4[0, 0], m4[0, 1], m4[0, 3]],
                                   [m4[1, 0], m4[1, 1], m4[1, 3]],
                                   [0.0, 0.0, 1.0]], dtype=np.float32)
                    t3 = getattr(mi, 'ScalarAffineTransform3f', None) \
                        or getattr(mi, 'ScalarTransform3f')
                    self.params[f'{prefix}.to_uv'] = t3(m3)
        self.params.update()
        if deferred_normals:
            for key, arr in deferred_normals:
                self.params[key] = mi.Float(arr)
            self.params.update()
        self.updates += 1

    # --- 入口 --------------------------------------------------------------
    def render(self, scene_dict: dict):
        import time as _time
        timing = os.environ.get('MRENDER_TIMING')
        t0 = _time.perf_counter()
        if self.scene is None:
            self._build(scene_dict)
            t1 = t2 = _time.perf_counter()
        else:
            diffs = self._diff(self.prev, scene_dict)
            actions = None
            if diffs is not None:
                actions = set()
                for p in diffs:
                    act = self._classify(p, scene_dict)
                    if act is None:
                        actions = None
                        break
                    actions.add(act)
            t1 = _time.perf_counter()
            if actions is None:
                self._build(scene_dict)
            elif actions:
                try:
                    self._apply(actions, scene_dict)
                except Exception:
                    # 未知の型変化などは安全側で作り直し
                    self._build(scene_dict)
            t2 = _time.perf_counter()
        self.prev = scene_dict
        image = mi.render(self.scene)
        if timing:
            import drjit as _dr
            _dr.eval(image)
            _dr.sync_thread()
            t3 = _time.perf_counter()
            print(f'    [timing] diff={t1 - t0:.3f}s '
                  f'apply/build={t2 - t1:.3f}s render={t3 - t2:.3f}s',
                  file=sys.stderr)
        return image


# ---------------------------------------------------------------------------
# フレーム並列: フレーム範囲を N チャンクに割ってサブプロセスでレンダ
# ---------------------------------------------------------------------------
def _run_parallel(args: argparse.Namespace, jobs: int) -> None:
    """--jobs N のオーケストレータ。

    GPU パストレは 1 フレームあたり一瞬で終わり、CPU 側のフレーム準備
    （シーン構築・PNG 書き出し等）が支配的なので、フレーム範囲を N 分割して
    同時に走らせるとほぼ線形にスケールする。各チャンクは同一の解決済み引数
    （JSON）+ frame_start/frame_end で独立実行され、tumble の姿勢伝播は
    各チャンクが frame 0 から決定論的に前進させるため直列実行とビット一致する。
    """
    import json
    import subprocess
    import time as _time

    n = max(1, min(int(jobs), int(args.frames)))
    output_dir = Path(args.output_dir or 'output/relative')
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = dict(vars(args))
    payload['output_dir'] = str(output_dir)
    payload['jobs'] = 1
    payload['config'] = []

    bounds = [round(i * args.frames / n) for i in range(n + 1)]
    tmp_dir = Path(tempfile.mkdtemp(prefix='mrender_jobs_'))
    procs = []
    t0 = _time.time()
    print(f'相対運動レンダリング（並列 {n} プロセス、frames={args.frames}）')
    for i in range(n):
        a, b = bounds[i], bounds[i + 1]
        if a >= b:
            continue
        chunk = dict(payload)
        chunk['frame_start'], chunk['frame_end'] = a, b
        path = tmp_dir / f'chunk_{i:02d}.json'
        path.write_text(json.dumps(chunk))
        procs.append((a, b, subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             '--args-file', str(path)],
            cwd=str(Path(__file__).resolve().parent))))
        print(f'  chunk {i}: frames [{a}, {b})')
    failed = []
    for a, b, p in procs:
        if p.wait() != 0:
            failed.append((a, b, p.returncode))
    if failed:
        raise RuntimeError(f'並列チャンクが失敗しました: {failed}')
    print(f'並列レンダ完了: {args.frames} frames / {_time.time() - t0:.1f}s '
          f'({n} プロセス) → {output_dir}/')


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

    高速化パス:
      - --jobs N (>1): フレーム範囲を N 分割してサブプロセス並列（_run_parallel）
      - 永続シーン（既定 ON、--no-persistent-scene で無効化）: mi.load_dict を
        毎フレームやり直さず、差分パラメータ更新のみでレンダ（PersistentScene）
    """
    frame_start = int(getattr(args, 'frame_start', None) or 0)
    frame_end_raw = getattr(args, 'frame_end', None)
    frame_end = int(frame_end_raw) if frame_end_raw else int(args.frames)
    is_chunk = getattr(args, 'frame_start', None) is not None

    if int(getattr(args, 'jobs', 1) or 1) > 1 and not is_chunk and args.frames > 1:
        _run_parallel(args, int(args.jobs))
        return

    output_dir = Path(args.output_dir or 'output/relative')
    output_dir.mkdir(parents=True, exist_ok=True)

    # dict 系（chief_parts / *_bsdf）は argparse に dest が無く YAML からしか
    # 来ないので getattr で拾う。それ以外は登録済み dest なので直接参照する。
    chief_parts = getattr(args, 'chief_parts', None)
    chief_default_bsdf = getattr(args, 'chief_bsdf', None)
    deputy_parts = getattr(args, 'deputy_parts', None)
    deputy_default_bsdf = getattr(args, 'deputy_bsdf', None)

    # chief 姿勢（全フレームで固定）
    chief_pos = chief_origin(args.chief_position)
    chief_q = np.asarray(args.chief_quat, dtype=float)
    chief_q = chief_q / np.linalg.norm(chief_q)
    chief_xform = make_body_transform(chief_pos, chief_q)

    # モード別の deputy 状態シーケンス生成
    csv_states = None
    if args.mode == 'csv':
        if not args.rel_csv:
            raise ValueError('--mode csv のときは --rel-csv が必須です')
        csv_states = load_csv_relative_state(args.rel_csv)

    abs_ctx = None
    if args.mode == 'absolute':
        abs_ctx = AbsoluteOrbitContext(args)

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
    if abs_ctx is not None and abs_ctx.deputy_attitude == 'tumble':
        # absolute+tumble はトルクフリー伝播を ECI 系で行う（RTN は回転系なので不可）
        q_dep = abs_ctx.initial_tumble_q_eci(start_t, rel_q_init)

    # --- 宇宙環境（太陽色・環境光・地球背景）---
    # static/csv/tumble ではフレーム間不変なので 1 度だけ構築。
    # absolute では太陽方向・食・地球姿勢が毎フレーム変わるため、ここでは
    # 太陽色と envmap だけ作り、地球はフレームループ内で orientation 付きで作る
    if abs_ctx is not None:
        env_args = argparse.Namespace(**vars(args))
        env_args.show_earth = False
        sun_rgb, env_dict, earth_dict = build_environment(env_args, chief_pos)
    else:
        sun_rgb, env_dict, earth_dict = build_environment(args, chief_pos)

    print('相対運動レンダリング')
    print(f"  モード: {args.mode}")
    print(f"  chief 位置: {chief_pos.tolist()}, クォータニオン: {chief_q.tolist()}")
    if args.mode == 'static':
        print(f"  rel_position: {rel_pos_init.tolist()}")
        print(f"  rel_quat:     {rel_q_init.tolist()}")
    elif args.mode == 'csv':
        print(f"  CSV: {args.rel_csv}  ({len(csv_states)} 行)")
    elif args.mode == 'absolute':
        print(f"  エポック: {args.epoch_utc} (JD {abs_ctx.jd0:.5f})")
        print(f"  chief 軌道:  {args.chief_orbit_csv or args.chief_oe}")
        print(f"  deputy 軌道: {args.deputy_orbit_csv or args.deputy_oe}")
        print(f"  deputy 姿勢: {abs_ctx.deputy_attitude}")
        print("  環境: 太陽方向 (VSOP87)・食・地球姿勢 (GMST) を毎フレーム更新")
    else:  # tumble
        print(f"  rel_position (固定): {rel_pos_init.tolist()}")
        print(f"  慣性テンソル: diag({args.Ix}, {args.Iy}, {args.Iz}) kg·m²")
        print(f"  初期角速度:   ({args.wx}, {args.wy}, {args.wz}) rad/s")
    span_s = dt * args.frames
    if args.duration_sec is not None:
        print(f"  時間軸: duration={args.duration_sec:.3f}s, dt={dt:.4f}s, start_t={start_t:.3f}s")
    else:
        print(f"  時間軸: fps={args.fps}, dt={dt:.4f}s, span={span_s:.3f}s, start_t={start_t:.3f}s")
    print(f"  フレーム数: {args.frames}"
          + (f"（このプロセスは [{frame_start}, {frame_end}) を担当）"
             if is_chunk else ''))
    print(f"  サンプル数: {args.samples} spp")
    print(f"  出力: {output_dir}/")
    print()

    # tumble 系はフレーム逐次伝播なので、チャンク担当分の手前まで決定論的に前進
    # （直列実行と同じ dt 列で propagate するため出力はビット一致する）
    tumble_active = args.mode == 'tumble' or (
        abs_ctx is not None and abs_ctx.deputy_attitude == 'tumble')
    if tumble_active:
        for _ in range(frame_start):
            q_dep, w_dep = propagate_attitude(q_dep, w_dep, I_body, I_inv, dt)

    persistent = (bool(getattr(args, 'persistent_scene', True))
                  and (frame_end - frame_start) > 1)
    pscene = PersistentScene() if persistent else None

    _timing = os.environ.get('MRENDER_TIMING')
    import time as _time

    for frame in range(frame_start, frame_end):
        t = start_t + frame * dt
        _t0 = _time.perf_counter()

        # フレーム別環境（absolute のみ時変。他モードは事前構築値をそのまま使う）
        frame_sun_direction = args.sun_direction
        frame_sun_rgb = sun_rgb
        frame_env_dict = env_dict
        frame_earth_dict = earth_dict
        nu = None

        if args.mode == 'static':
            rel_pos = rel_pos_init
            rel_q = rel_q_init
        elif args.mode == 'csv':
            rel_pos, rel_q = interp_csv_state(csv_states, t, csv_label=args.rel_csv)
        elif args.mode == 'absolute':
            rel_pos, a_i2rtn, r_c, _v_c = abs_ctx.relative_state(t)
            a_eci2body = abs_ctx.deputy_attitude_dcm(
                t, a_i2rtn, rel_q_init,
                q_dep if abs_ctx.deputy_attitude == 'tumble' else None)
            rel_q = abs_ctx.rel_quat_scene(a_eci2body, a_i2rtn)
            nu, sun_dir_scene, _ecef2scene, frame_earth_dict = \
                abs_ctx.environment_at(t, r_c, a_i2rtn, args)
            frame_sun_direction = (-sun_dir_scene).tolist()
            frame_sun_rgb = [c * nu for c in sun_rgb]
            frame_env_dict = abs_ctx.rotate_env_dict(env_dict, a_i2rtn)
        else:  # tumble
            rel_pos = rel_pos_init
            rel_q = q_dep

        deputy_pos_inertial, deputy_q_inertial = scene_deputy_state(args, chief_q, rel_pos, rel_q)

        deputy_xform = make_body_transform(deputy_pos_inertial, deputy_q_inertial)
        _t1 = _time.perf_counter()

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
            sun_direction=frame_sun_direction,
            sun_rgb=frame_sun_rgb,
            env_dict=frame_env_dict,
            earth_dict=frame_earth_dict,
            hide_chief=bool(args.hide_chief), camera_mode=args.camera_mode,
            camera_offset=args.camera_offset, camera_direction=args.camera_direction,
            camera_body_up=args.camera_body_up,
        )
        _t2 = _time.perf_counter()
        if pscene is not None:
            # 永続シーン: 初回のみ構築、以後は差分パラメータ更新でレンダ
            image = pscene.render(scene_dict)
        else:
            # テクスチャ/星空 EXR の毎フレーム再デコードを避ける
            # （BSDF テクスチャは実体共有、envmap 等は Bitmap キャッシュ。
            #  live_worker のリファインパスと同一経路になり、ライブ⇔バッチの
            #  ピクセル一致が構成上保証される）
            from asset_cache import preload_bitmaps, share_bsdf_textures
            scene = mi.load_dict(preload_bitmaps(share_bsdf_textures(scene_dict)))
            image = mi.render(scene)

        _t3 = _time.perf_counter()
        output_path = output_dir / f'frame_{frame:04d}.png'
        mi.util.write_bitmap(str(output_path), image)
        if _timing:
            _t4 = _time.perf_counter()
            print(f'    [timing] env/state={_t1 - _t0:.3f}s dict={_t2 - _t1:.3f}s '
                  f'pscene={_t3 - _t2:.3f}s write={_t4 - _t3:.3f}s',
                  file=sys.stderr)

        rel_dist = float(np.linalg.norm(rel_pos))
        nu_note = f"  ν={nu:.3f}" if nu is not None else ''
        print(f"  [{frame+1:3d}/{args.frames}] t={t:.3f}s  "
              f"|r_rel|={rel_dist:.3f}  "
              f"q_rel=[{rel_q[0]:.3f},{rel_q[1]:.3f},{rel_q[2]:.3f},{rel_q[3]:.3f}]"
              f"{nu_note}  → {output_path}")

        # tumble（および absolute の deputy_attitude=tumble）のみ次フレームへ姿勢伝播
        if args.mode == 'tumble' or (
                abs_ctx is not None and abs_ctx.deputy_attitude == 'tumble'):
            q_dep, w_dep = propagate_attitude(q_dep, w_dep, I_body, I_inv, dt)

    if pscene is not None:
        print(f"\n永続シーン: 構築 {pscene.rebuilds} 回 / 差分更新 {pscene.updates} 回")
    print(f"\n完了！ 動画にするには:")
    print(f"  ffmpeg -framerate {int(args.fps)} -i {output_dir}/frame_%04d.png "
          f"-c:v libx264 -pix_fmt yuv420p output/relative_motion.mp4")


def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    # --jobs 並列のチャンク子プロセス入口: 解決済み引数 JSON を直接ロード
    # （argparse を経由しないので dict 値の deputy_parts 等もそのまま届く）
    if len(argv) == 2 and argv[0] == '--args-file':
        import json
        args = argparse.Namespace(**json.loads(Path(argv[1]).read_text()))
        _run(args)
        return
    args = parse_args(argv)
    _run(args)


if __name__ == '__main__':
    main()
