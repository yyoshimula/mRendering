#!/usr/bin/env python3
"""ライブプレビュー用の常駐レンダーワーカー（全 verb 対応、v2）。

Blender のレンダープレビューのように「パラメータを変えたら即座に描き直す」
ための小さな HTTP サーバー。Mitsuba を 1 回だけ import して常駐し、
専用スレッドで最新の希望状態（desired state）だけを描き続ける。

設計方針:
  - **原則フルリビルド**。ただし操作中（interactive）にカメラだけが動く
    オービット/パン/ドリーは、毎更新の mi.load_dict（メッシュ再ロード +
    BVH 再構築、oos_hubble で ~140 ms）が支配項になるので、
    **カメラ高速パス**（下の CameraCache / camera_from_fields 参照）で
    ロード済みシーンを保持し `mi.traverse` の 'camera.to_world' 差し替えだけで
    描き直す。それ以外（フィールド変更・フレーム変更・refine）は従来どおり
    毎回フルリビルドする。
  - **画作りはバッチ実装の関数をそのまま再利用**する。verb ごとに
    「fields → Namespace → シーン辞書 → PNG」の経路をバッチ側と同じ順序・
    同じ式で組み立てるので、同一パラメータ・同一 spp・同一解像度なら
    リファインパスの出力はバッチ結果とピクセル一致する。
      * relative → relative_motion.build_scene / make_body_transform / ...
      * rotation → simple_rotation.build_scene / propagate_attitude / q2dcm
      * render 系（render/preview/onboard/lightcurve）
                → satellite_orbit.render_frame を分解して再利用
                  （compute_frame_time → resolve_sun_direction →
                    compute_object_state → build_scene_objects →
                    resolve_camera → build_earth_textures →
                    scene_builder.create_scene → apply_tonemap）
  - **argparse デフォルトを唯一の真実**にする。fields は各 verb の
    `build_parser().parse_args([])` で作った Namespace に上書きするので、
    verb 側のデフォルトとズレない。
  - Mitsuba を触るのはレンダースレッド 1 本だけ。HTTP ハンドラは状態の
    読み書きしかしない。
  - **キャッシュ**（下記各所参照）で 1 更新あたりの再計算を最小化する:
    GLB→OBJ 変換 / mi.Bitmap / CSV / 姿勢軌跡 / render 系のセットアップ
    （RenderConfig + ObjectSpec）/ 地球テクスチャ / show_orbit の軌跡位置列。
    OBJ パーツ分割のキャッシュは scene_common.split_obj_by_parts 側に内蔵
    （バッチ実行でも効くので、ここで monkeypatch する必要はない）。

起動:
    python live_worker.py --port 8601 [--host 127.0.0.1]

cwd はリポジトリルート前提（'earth_texture.jpg' 等の相対パスを解決するため）。
gui_server.py からは cwd=ROOT で spawn される。

HTTP API（すべて JSON、エラーは {"error": msg}）:
    POST /update    {verb, fields, frame, quality} → {"ok": true, "gen": N}
                    quality.interactive=true で「操作中の簡易描画」
                    （下の simplify_fields / simplify_scene 参照）。既定 false。
    GET  /frame?known_gen=N[&known_refined=0|1] → PNG (200) / 304 / 404
                    レスポンスヘッダ X-Live-Simplified: 0|1 で簡易化の有無
    GET  /status    → 状態スナップショット（camera / time_s / simplified /
                      fast_path を含む）
    POST /shutdown  → プロセス終了
    GET  /health    → {"ok": true}
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# relative_motion の import 時点で init_variant()（MRENDER_VARIANT 環境変数、
# 既定 llvm_ad_rgb）がバリアントを設定する
import relative_motion as RM  # noqa: E402
import scene_common as SC  # noqa: E402
import mitsuba as mi  # noqa: E402

SCALAR = RM.SCALAR

# ライブプレビュー対応 verb。
#   relative / rotation は専用パーサ + 専用シーンビルダーを持つ。
#   ORBIT_VERBS は satellite_orbit + config_loader の共通パイプラインで、
#   lightcurve はライブでは「シーン画像プレビュー」として render と同一扱い
#   （光度曲線 CSV はバッチ実行でのみ計算する）。onboard は GUI 側 defaults の
#   view_mode=satellite が fields に載ってくるだけで、経路は render と同じ。
ORBIT_VERBS = ('render', 'preview', 'onboard', 'lightcurve')
SUPPORTED_VERBS = ('relative', 'rotation', 'groundobs') + ORBIT_VERBS

# refine（高品質再レンダ）の解像度上限
REFINE_MAX_W = 1280
REFINE_MAX_H = 960
MIN_RENDER_PX = 64


# ---------------------------------------------------------------------------
# 重いモジュールの遅延 import
# ---------------------------------------------------------------------------
# relative しか使わないセッションで simple_rotation / satellite_orbit（＋
# optical_* / scene_* 一式）を読み込まないようにする。どちらも import 時に
# init_variant() を呼ぶが、設定済みなら no-op なので無害。
_MOD_LOCK = threading.Lock()
_MODS: Dict[str, Any] = {}


def rotation_mod():
    """simple_rotation を遅延 import して返す。

    OBJ パーツ分割のキャッシュは scene_common.split_obj_by_parts に内蔵されて
    いる（relative / rotation が同じ実装を共有する）ので、ここでの注入は不要。
    """
    with _MOD_LOCK:
        mod = _MODS.get('rotation')
        if mod is None:
            import simple_rotation as SR  # noqa: N806
            _MODS['rotation'] = mod = SR
        return mod


def orbit_mod():
    """render 系のモジュール束（satellite_orbit / verbs._common / scene_builder）。"""
    with _MOD_LOCK:
        mod = _MODS.get('orbit')
        if mod is None:
            import satellite_orbit as SO  # noqa: N806
            import scene_builder as SB  # noqa: N806
            from verbs import _common as VC  # noqa: N806
            patch_glb_cache(SO)
            _MODS['orbit'] = mod = types.SimpleNamespace(SO=SO, SB=SB, VC=VC)
        return mod


# ---------------------------------------------------------------------------
# GLB/glTF → OBJ 変換のキャッシュ
# ---------------------------------------------------------------------------
# scene_objects.load_external_model は .glb/.gltf を毎回 trimesh で OBJ へ
# 落とす（実測 models/iss.glb で 0.9 s/回）。ライブでは 1 更新ごとにこれが
# 走るので致命的。(path, mtime) でメモ化し、変換済み OBJ が既にあって
# GLB より新しければ再変換しない。
_GLB_CACHE: Dict[Tuple[str, float], str] = {}
_GLB_LOCK = threading.Lock()


def glb_to_obj_cached(path: Path) -> Path:
    """GLB/glTF を OBJ へ変換して返す（既存の新しい OBJ があれば再利用）。

    変換式は scene_objects.load_external_model と同一（Scene なら全 Trimesh を
    concatenate、include_normals=False で export）なので、生成される OBJ の
    中身はバッチ実行時と同じになる。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError as exc:
        raise ValueError(f'モデルが読めません: {path} ({exc})') from exc
    key = (str(path), mtime)
    with _GLB_LOCK:
        hit = _GLB_CACHE.get(key)
    if hit is not None and os.path.exists(hit):
        return Path(hit)

    obj_path = path.with_suffix('.obj')
    if not (obj_path.exists() and os.path.getmtime(obj_path) >= mtime):
        import trimesh
        scene_or_mesh = trimesh.load(str(path))
        if isinstance(scene_or_mesh, trimesh.Scene):
            meshes = [g for g in scene_or_mesh.geometry.values()
                      if isinstance(g, trimesh.Trimesh)]
            if not meshes:
                raise ValueError(f'GLBファイルにメッシュが含まれていません: {path}')
            mesh = trimesh.util.concatenate(meshes)
        else:
            mesh = scene_or_mesh
        mesh.export(str(obj_path), file_type='obj', include_normals=False)
        print(f'live: [trimesh] {path} → {obj_path}', flush=True)

    with _GLB_LOCK:
        _GLB_CACHE[key] = str(obj_path)
    return obj_path


def patch_glb_cache(module: Any) -> None:
    """`module.load_external_model` を GLB 変換キャッシュ付きに差し替える。

    satellite_orbit は `from scene_objects import load_external_model` で
    自モジュールの名前空間に束縛しているので、satellite_orbit 側の属性を
    差し替える必要がある（scene_objects 側だけでは効かない）。
    """
    orig = module.load_external_model

    def _cached_load_external_model(filepath: str, *args: Any, **kwargs: Any):
        suffix = Path(filepath).suffix.lower()
        if suffix in ('.glb', '.gltf'):
            filepath = str(glb_to_obj_cached(Path(filepath)))
        return orig(filepath, *args, **kwargs)

    module.load_external_model = _cached_load_external_model


# ---------------------------------------------------------------------------
# ビットマップのプリロード（毎回の mi.load_dict でのデコードを避ける）
# ---------------------------------------------------------------------------
# シーン辞書中の {'type': 'bitmap'|'envmap', 'filename': ...} を
# {'type': ..., 'bitmap': <mi.Bitmap>} に差し替える。Mitsuba 側の処理は
# 同一経路なので画はビット単位でほぼ一致する（実測 max|diff| = 3.6e-7 = float32 誤差）。
#
# ⚠ 読み込み済みの *emitter オブジェクト* をシーン間で使い回すのは不可。
#    1 つの emitter が複数シーンに属すると内部状態が壊れ、実測で
#    mean|diff| がリファレンスの ~27% ずれた。共有してよいのは Bitmap まで。
_BITMAP_CACHE: Dict[Tuple[str, float, int], Any] = {}
_BITMAP_LOCK = threading.Lock()

# これを超える画素数のビットマップはキャッシュしない（メモリ肥大の防止）
BITMAP_CACHE_MAX_PIXELS = 20_000_000
# プレビュー品質で使う envmap の最大幅。assets/starfield.exr は 8192x4096 で、
# envmap プラグインのサンプリング分布構築だけで ~840 ms かかる。プレビューでは
# 縮小して応答性を優先し、refine パスでは原寸に戻す（下の render_once 参照）。
PREVIEW_ENV_MAX_WIDTH = 2048
# プレビュー品質で使う通常テクスチャ（地球昼/夜/雲）の最大幅。8K テクスチャの
# デコード後リサンプルは 1 度だけなのでキャッシュが効くが、Mitsuba 側の
# テクスチャ構築コストが解像度に比例するため縮めておく。
# ⚠ refine パスでは必ず 0（原寸）を渡すこと。バッチ出力とのピクセル一致は
#    原寸でのみ保証される。
PREVIEW_TEX_MAX_WIDTH = 2048


def load_bitmap(path: str, max_width: int = 0):
    """画像を mi.Bitmap として読み込む（(path, mtime, max_width) でキャッシュ）。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError as exc:
        raise ValueError(f'画像が読めません: {path} ({exc})') from exc
    key = (str(path), mtime, int(max_width))
    with _BITMAP_LOCK:
        hit = _BITMAP_CACHE.get(key)
    if hit is not None:
        return hit

    bmp = mi.Bitmap(path)
    size = bmp.size()
    if max_width and size[0] > max_width:
        # Bitmap.resample() は float16/32/64 のみ対応。JPEG/PNG は UInt8 で
        # 読まれるので Float32 化してから縮小する。srgb_gamma フラグは維持
        # するので、Mitsuba 側の sRGB デコード経路は原寸と同じになる。
        was_ldr = bmp.component_format() not in (mi.Struct.Type.Float16,
                                                 mi.Struct.Type.Float32,
                                                 mi.Struct.Type.Float64)
        if was_ldr:
            bmp = bmp.convert(bmp.pixel_format(), mi.Struct.Type.Float32, bmp.srgb_gamma())
        new_h = max(1, round(size[1] * max_width / size[0]))
        # clamp: 再構成フィルタのリンギングで [0,1] を超えると
        # BitmapTexture が警告を出す（LDR 由来のテクスチャのみ 1.0 で抑える）。
        clamp = (0.0, 1.0) if was_ldr else (0.0, float('inf'))
        bmp = bmp.resample([int(max_width), int(new_h)], None,
                           (mi.FilterBoundaryCondition.Clamp,
                            mi.FilterBoundaryCondition.Clamp), clamp)
        size = bmp.size()

    if size[0] * size[1] <= BITMAP_CACHE_MAX_PIXELS:
        with _BITMAP_LOCK:
            _BITMAP_CACHE[key] = bmp
            if len(_BITMAP_CACHE) > 24:
                _BITMAP_CACHE.pop(next(iter(_BITMAP_CACHE)))
    return bmp


def preload_bitmaps(node: Any, env_max_width: int = 0, tex_max_width: int = 0) -> Any:
    """シーン辞書を再帰的に走査し、filename 指定の画像を Bitmap 実体に差し替える。

    env_max_width は 'envmap'、tex_max_width は 'bitmap' に対する幅の上限
    （0 = 原寸）。relative verb は従来どおり tex_max_width=0 で呼ばれるので
    画は変わらない。
    """
    if isinstance(node, dict):
        ntype = node.get('type')
        filename = node.get('filename')
        if ntype in ('bitmap', 'envmap') and isinstance(filename, str):
            max_w = env_max_width if ntype == 'envmap' else tex_max_width
            try:
                bmp = load_bitmap(filename, max_w)
            except Exception:  # noqa: BLE001 - 読めなければ Mitsuba 側に任せる
                return node
            out = {k: v for k, v in node.items() if k != 'filename'}
            out['bitmap'] = bmp
            return out
        return {k: preload_bitmaps(v, env_max_width, tex_max_width) for k, v in node.items()}
    if isinstance(node, list):
        return [preload_bitmaps(v, env_max_width, tex_max_width) for v in node]
    return node


# ---------------------------------------------------------------------------
# fields(dict) → argparse Namespace
# ---------------------------------------------------------------------------
def _action_meta(parser: argparse.ArgumentParser) -> Dict[str, Tuple[Any, Any, Any]]:
    """argparse から dest → (type, nargs, default) を抽出する。

    GUI/JSON からは数値が文字列で来ることもあるので、この情報で型を揃える。
    """
    meta: Dict[str, Tuple[Any, Any, Any]] = {}
    for action in parser._actions:  # noqa: SLF001 - 型情報の取得のみ
        meta[action.dest] = (action.type, action.nargs, action.default)
    return meta


def _defaults_meta(namespace: argparse.Namespace) -> Dict[str, Tuple[Any, Any, Any]]:
    """`set_defaults` だけで既定値を持つパーサ用の型メタを既定値から推定する。

    config_loader.build_parser() は `--config` 以外の個別フラグを登録しない
    （既定値は _ARG_DEFAULTS を set_defaults で流し込む）ので、action からは
    型が取れない。既定値そのものの型を使う。
    """
    meta: Dict[str, Tuple[Any, Any, Any]] = {}
    for dest, default in vars(namespace).items():
        if isinstance(default, bool):
            typ: Any = None            # bool は _coerce 側で default から判定
        elif isinstance(default, int):
            typ = int
        elif isinstance(default, float):
            typ = float
        else:
            typ = None
        nargs = '+' if isinstance(default, (list, tuple)) else None
        meta[dest] = (typ, nargs, default)
    return meta


def _coerce(meta: Dict[str, Tuple[Any, Any, Any]], dest: str, value: Any) -> Any:
    """argparse の type/nargs 情報に合わせて JSON 値をキャストする。"""
    if value is None:
        return None
    typ, nargs, default = meta.get(dest, (None, None, None))
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() not in ('', '0', 'false', 'no', 'off')
        return bool(value)
    if typ in (int, float):
        try:
            if isinstance(value, (list, tuple)):
                return [typ(v) for v in value]
            if nargs in (None, '?'):
                return typ(value)
            return value
        except (TypeError, ValueError):
            return value
    return value


def flatten_fields(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """fields のキーを dest 名へ正規化し、`advanced:` の包みを展開する。

    `advanced:` は gui_server.compose_config が dict 値（model_parts 等）を
    包むために作るセクション。どちらの YAML ローダも 1 段フラット化するので
    ここでも同じように展開する。
    """
    flat: Dict[str, Any] = {}
    for key, value in (fields or {}).items():
        k = str(key).replace('-', '_')
        if k == 'advanced' and isinstance(value, dict):
            for sub_k, sub_v in value.items():
                flat[str(sub_k).replace('-', '_')] = sub_v
        else:
            flat[k] = value
    return flat


def apply_fields(args: argparse.Namespace, fields: Optional[Dict[str, Any]],
                 meta: Dict[str, Tuple[Any, Any, Any]]) -> argparse.Namespace:
    """fields を Namespace へ setattr する（型は meta に合わせて強制）。"""
    for key, value in flatten_fields(fields).items():
        setattr(args, key, _coerce(meta, key, value))
    return args


_RELATIVE_META = _action_meta(RM.build_parser())


def make_args(fields: Optional[Dict[str, Any]]) -> argparse.Namespace:
    """fields（relative の argparse dest 名のフラット辞書）を Namespace 化する。

    未指定キーは argparse のデフォルトのままなので、relative verb と
    完全に同じ既定値になる。dict 値（deputy_parts / deputy_bsdf /
    chief_parts / chief_bsdf）はそのまま属性として載る。
    """
    return apply_fields(RM.build_parser().parse_args([]), fields, _RELATIVE_META)


# ---------------------------------------------------------------------------
# 時間軸・相対状態（relative_motion._run() と同一ロジック）
# ---------------------------------------------------------------------------
def compute_dt(args: argparse.Namespace) -> float:
    """dt の決め方は _run() と同一: duration_sec 優先、なければ 1/fps。"""
    duration = args.duration_sec
    frames = int(args.frames or 0)
    if duration is not None and float(duration) > 0 and frames > 0:
        return float(duration) / frames
    fps = float(args.fps or 0.0)
    return 1.0 / fps if fps > 0 else 1.0


_CSV_CACHE: Dict[Tuple[str, float], List[Tuple[float, np.ndarray, np.ndarray]]] = {}
# relative(tumble) と rotation の姿勢軌跡を 1 つの辞書で持つ。キーの先頭に
# verb タグを入れてあるので、同じパラメータでも verb 間で混ざらない。
_TRAJ_CACHE: Dict[tuple, List[np.ndarray]] = {}
_TRAJ_LOCK = threading.Lock()


def load_csv_cached(path: str):
    """相対状態 CSV を (path, mtime) キーでキャッシュして読み込む。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError as exc:
        raise ValueError(f'CSV が読めません: {path} ({exc})') from exc
    key = (str(path), mtime)
    hit = _CSV_CACHE.get(key)
    if hit is None:
        hit = RM.load_csv_relative_state(path)
        _CSV_CACHE[key] = hit
    return hit


# mode=absolute の軌道コンテキスト（CSV ロード・エポック解析を含むので
# 軌道系フィールドをキーにキャッシュ。フォームのカメラ・材質変更では作り直さない）
_ABS_CACHE: Dict[tuple, Any] = {}


def _mtime_or_zero(path) -> float:
    try:
        return os.path.getmtime(str(path)) if path else 0.0
    except OSError:
        return 0.0


def absolute_context(args: argparse.Namespace):
    """relative mode=absolute の AbsoluteOrbitContext をキャッシュ付きで返す。"""
    key = (
        str(args.epoch_utc),
        tuple(float(v) for v in (args.chief_oe or [])),
        tuple(float(v) for v in args.deputy_oe) if getattr(args, 'deputy_oe', None) else None,
        str(getattr(args, 'chief_orbit_csv', None) or ''),
        _mtime_or_zero(getattr(args, 'chief_orbit_csv', None)),
        str(getattr(args, 'deputy_orbit_csv', None) or ''),
        _mtime_or_zero(getattr(args, 'deputy_orbit_csv', None)),
        str(getattr(args, 'deputy_attitude', 'tumble')),
    )
    hit = _ABS_CACHE.get(key)
    if hit is None:
        hit = RM.AbsoluteOrbitContext(args)
        _ABS_CACHE[key] = hit
        if len(_ABS_CACHE) > 4:
            _ABS_CACHE.pop(next(iter(_ABS_CACHE)))
    return hit


def attitude_trajectory(verb: str, args: argparse.Namespace, q0: np.ndarray,
                        dt: float, n_frames: int) -> List[np.ndarray]:
    """全フレームの姿勢クォータニオン列を事前計算してキャッシュする。

    relative(tumble) の deputy 姿勢と rotation の機体姿勢は、初期値
    （relative: rel_quat / rotation: 恒等）が違うだけで伝播規則は同一なので、
    `scene_common.propagate_trajectory` の 1 実装を共有する。バッチ側の
    フレームループと同じ順序・同じ刻みなので traj[k] はバッチのフレーム k と
    一致し、フレームスクラブが O(1) になる。
    """
    key = (
        verb,
        float(args.Ix), float(args.Iy), float(args.Iz),
        float(args.wx), float(args.wy), float(args.wz),
        tuple(float(v) for v in np.asarray(q0, dtype=float).reshape(-1)),
        int(n_frames), float(dt),
    )
    with _TRAJ_LOCK:
        hit = _TRAJ_CACHE.get(key)
    if hit is not None:
        return hit

    I_body, I_inv = SC.inertia_matrices(float(args.Ix), float(args.Iy), float(args.Iz))
    w0 = np.array([float(args.wx), float(args.wy), float(args.wz)], dtype=float)
    traj = SC.propagate_trajectory(q0, w0, I_body, I_inv, dt, n_frames)

    with _TRAJ_LOCK:
        _TRAJ_CACHE[key] = traj
        if len(_TRAJ_CACHE) > 16:  # 無制限に増やさない
            _TRAJ_CACHE.pop(next(iter(_TRAJ_CACHE)))
    return traj


def relative_state_at(args: argparse.Namespace, frame: int) -> Tuple[np.ndarray, np.ndarray, float, int]:
    """フレーム `frame` の (rel_pos, rel_q, t, traj_frames) を返す。

    _run() のフレームループ ① と同じ:
        static: 初期値固定 / csv: 時刻 t を補間 / tumble: 姿勢動力学
    """
    dt = compute_dt(args)
    start_t = float(args.start_time or 0.0)
    n_frames = max(1, int(args.frames or 1))
    frame = max(0, min(int(frame), n_frames - 1))
    t = start_t + frame * dt

    rel_pos = np.asarray(args.rel_position, dtype=float)
    rel_q_init = np.asarray(args.rel_quat, dtype=float)
    rel_q_init = rel_q_init / np.linalg.norm(rel_q_init)

    if args.mode == 'csv':
        if not args.rel_csv:
            raise ValueError('mode=csv のときは rel_csv が必須です')
        states = load_csv_cached(args.rel_csv)
        rel_pos, rel_q = RM.interp_csv_state(states, t, csv_label=str(args.rel_csv))
        return rel_pos, rel_q, t, len(states)
    if args.mode == 'tumble':
        traj = attitude_trajectory('relative', args, rel_q_init, dt, n_frames)
        return rel_pos, traj[frame], t, len(traj)
    if args.mode == 'absolute':
        # バッチ (_run) の absolute 分岐と同じ計算（AbsoluteOrbitContext を共有）。
        # tumble 姿勢は ECI 系で伝播した軌跡をフレームスクラブ用にキャッシュ
        ctx = absolute_context(args)
        rel_pos, a_i2rtn, _r_c, _v_c = ctx.relative_state(t)
        if ctx.deputy_attitude == 'tumble':
            q0 = ctx.initial_tumble_q_eci(start_t, rel_q_init)
            traj = attitude_trajectory('relative_abs', args, q0, dt, n_frames)
            a_eci2body = ctx.deputy_attitude_dcm(t, a_i2rtn, rel_q_init, traj[frame])
        else:
            a_eci2body = ctx.deputy_attitude_dcm(t, a_i2rtn, rel_q_init, None)
        rel_q = ctx.rel_quat_scene(a_eci2body, a_i2rtn)
        return rel_pos, rel_q, t, n_frames
    return rel_pos, rel_q_init, t, n_frames


# ---------------------------------------------------------------------------
# シーン辞書（_run() のフレームループと同じ関数を呼ぶ）
# ---------------------------------------------------------------------------
def build_scene_dict(args: argparse.Namespace, frame: int,
                     width: int, height: int, samples: int) -> Tuple[dict, int, float]:
    """1 フレーム分の Mitsuba シーン辞書を作る（_run() のフレームループ ②③）。

    Returns:
        (scene_dict, traj_frames, time_s)
    """
    # dict 系（*_parts / *_bsdf）は argparse に dest が無く fields からしか
    # 来ないので getattr で拾う。それ以外は登録済み dest なので直接参照する。
    chief_parts = getattr(args, 'chief_parts', None)
    chief_default_bsdf = getattr(args, 'chief_bsdf', None)
    deputy_parts = getattr(args, 'deputy_parts', None)
    deputy_default_bsdf = getattr(args, 'deputy_bsdf', None)

    chief_pos = np.asarray(args.chief_position, dtype=float)
    chief_q = np.asarray(args.chief_quat, dtype=float)
    chief_q = chief_q / np.linalg.norm(chief_q)
    chief_xform = RM.make_body_transform(chief_pos, chief_q)

    rel_pos, rel_q, time_s, traj_frames = relative_state_at(args, frame)

    if args.mode == 'absolute':
        # シーン = chief RTN。rel_pos/rel_q は既に RTN 基準（バッチと同じ）
        deputy_pos_inertial, deputy_q_inertial = rel_pos, rel_q
    else:
        # deputy の慣性位置・姿勢はバッチ (_run) と同じ共有ヘルパーで合成する
        # （規約・数値検証は relative_motion.compose_deputy_state の docstring 参照）
        deputy_pos_inertial, deputy_q_inertial = RM.compose_deputy_state(
            chief_pos, chief_q, rel_pos, rel_q)

    deputy_xform = RM.make_body_transform(deputy_pos_inertial, deputy_q_inertial)
    # 太陽色・環境光・地球背景もバッチ (_run) と同じ共有ヘルパーで組む
    sun_direction = args.sun_direction
    if args.mode == 'absolute':
        # 時変環境（バッチの absolute 分岐と同一計算）: 太陽方向 (VSOP87)・
        # 食 ν・地球姿勢 (GMST + 直下点クロップ)・星空 ECI 固定
        ctx = absolute_context(args)
        _rp, a_i2rtn, r_c, _vc = ctx.relative_state(time_s)
        env_args = argparse.Namespace(**vars(args))
        env_args.show_earth = False
        sun_rgb, env_dict, _ = RM.build_environment(env_args, chief_pos)
        nu, sun_dir_scene, _e2s, earth_dict = ctx.environment_at(
            time_s, r_c, a_i2rtn, args)
        sun_direction = (-sun_dir_scene).tolist()
        sun_rgb = [c * nu for c in sun_rgb]
        env_dict = RM.AbsoluteOrbitContext.rotate_env_dict(env_dict, a_i2rtn)
    else:
        sun_rgb, env_dict, earth_dict = RM.build_environment(args, chief_pos)

    scene_dict = RM.build_scene(
        chief_xform, deputy_xform,
        width=width, height=height, samples=samples,
        chief_model=args.chief_model, chief_parts=chief_parts,
        chief_default_bsdf=chief_default_bsdf, chief_scale=float(args.chief_scale),
        deputy_model=args.deputy_model, deputy_parts=deputy_parts,
        deputy_default_bsdf=deputy_default_bsdf, deputy_scale=float(args.deputy_scale),
        camera_origin=args.camera_origin, camera_target=args.camera_target,
        camera_fov=float(args.camera_fov),
        show_inertial_axes=bool(args.show_inertial_axes),
        show_body_axes=bool(args.show_body_axes),
        camera_up=args.camera_up,
        max_depth=int(args.max_depth),
        sun_direction=sun_direction,
        sun_rgb=sun_rgb,
        env_dict=env_dict,
        earth_dict=earth_dict,
        hide_chief=bool(args.hide_chief),
    )
    return scene_dict, traj_frames, time_s


# ---------------------------------------------------------------------------
# verb 共通のフレーム構築結果
# ---------------------------------------------------------------------------
class FrameBuild(NamedTuple):
    """1 フレーム分の「レンダ直前」の状態。

    scene_dict: mi.load_dict に渡す辞書（Bitmap 差し替え前）
    traj_frames: フレームスクラブ用の総フレーム数
    camera: 解決済みカメラ {origin, target, up, fov}（シーン単位）。/status へ返す
    time_s: そのフレームの物理時刻 [s]
    tonemap: (exposure, gamma) を返すと apply_tonemap 経路で PNG 化する。
             None なら mi.util.write_bitmap（既定 sRGB 変換）経路。
    timings: ステージ別の所要時間 [ms]（ログ用）
    """

    scene_dict: dict
    traj_frames: int
    camera: Optional[Dict[str, Any]]
    time_s: float
    tonemap: Optional[Tuple[float, float]]
    timings: Dict[str, float]
    # groundobs 用の特殊経路: verb 側で処理済みの表示用 [0,1] 画像。
    # これが載っていると worker は mi.load_dict / mi.render をスキップし、
    # この画像をそのまま PNG 化する（センサ後処理を含む画のため）。
    image01: Optional[np.ndarray] = None


def _vec(value: Any, fallback: Sequence[float]) -> List[float]:
    """カメラ値などを JSON 化可能な float リストへ落とす。"""
    if value is None:
        value = fallback
    return [float(v) for v in np.asarray(value, dtype=float).reshape(-1)[:3]]


def build_relative_frame(fields: Dict[str, Any], frame: int,
                         width: int, height: int, samples: int) -> FrameBuild:
    """relative verb（v1 からの既存経路）。画作りは一切変更していない。"""
    t0 = time.perf_counter()
    args = make_args(fields)
    scene_dict, traj_frames, time_s = build_scene_dict(args, frame, width, height, samples)
    camera = {
        'origin': _vec(args.camera_origin, [3.5, 2.5, 2.0]),
        'target': _vec(args.camera_target, [0.5, 0.0, 0.0]),
        'up': _vec(args.camera_up, [0.0, 1.0, 0.0]),
        'fov': float(args.camera_fov or 45.0),
    }
    return FrameBuild(scene_dict, traj_frames, camera, time_s, None,
                      {'build': (time.perf_counter() - t0) * 1000.0})


# ---------------------------------------------------------------------------
# rotation verb（simple_rotation の再利用）
# ---------------------------------------------------------------------------
_ROT_META: Optional[Dict[str, Tuple[Any, Any, Any]]] = None
# 姿勢軌跡は relative(tumble) と共通の attitude_trajectory / _TRAJ_CACHE を使う。


def rotation_args(fields: Optional[Dict[str, Any]]) -> argparse.Namespace:
    """fields を simple_rotation の argparse dest 空間の Namespace にする。"""
    global _ROT_META
    SR = rotation_mod()  # noqa: N806
    parser = SR.build_parser()
    if _ROT_META is None:
        _ROT_META = _action_meta(parser)
    return apply_fields(parser.parse_args([]), fields, _ROT_META)


def build_rotation_frame(fields: Dict[str, Any], frame: int,
                         width: int, height: int, samples: int) -> FrameBuild:
    """rotation verb: simple_rotation._run() のフレームループ ①②③ と同一。"""
    t0 = time.perf_counter()
    SR = rotation_mod()  # noqa: N806
    args = rotation_args(fields)

    n_frames = max(1, int(args.frames or 1))
    frame = max(0, min(int(frame), n_frames - 1))
    fps = float(args.fps or 30.0)
    dt = 1.0 / fps if fps > 0 else 1.0

    # simple_rotation._run() と同じ初期条件（q=[0,0,0,1]、w=(wx,wy,wz)、dt=1/fps）
    traj = attitude_trajectory('rotation', args, np.array([0.0, 0.0, 0.0, 1.0]),
                               dt, n_frames)
    # ①② q (inertial→body) → to_world (body→inertial = A(q)ᵀ)。
    # バッチ (_run) と同じ共有ヘルパーを使い、転置の実装を 1 箇所に閉じ込める。
    body_transform = SR.quat_to_body_transform(traj[frame])

    # ③ シーン辞書。model_parts / model_bsdf は argparse に dest が無く
    #    fields からしか来ないので getattr、それ以外は直接参照。
    scene_dict = SR.build_scene(
        body_transform, width, height, samples,
        model_path=args.model_path,
        part_bsdfs=getattr(args, 'model_parts', None),
        default_bsdf=getattr(args, 'model_bsdf', None),
        model_scale=float(args.model_scale),
        camera_origin=args.camera_origin,
        camera_target=args.camera_target,
        camera_fov=float(args.camera_fov),
    )
    camera = {
        'origin': _vec(args.camera_origin, [3.0, 2.0, 2.0]),
        'target': _vec(args.camera_target, [0.0, 0.0, 0.0]),
        # simple_rotation.build_scene は up=[0,1,0] 固定
        'up': [0.0, 1.0, 0.0],
        'fov': float(args.camera_fov),
    }
    return FrameBuild(scene_dict, n_frames, camera, frame * dt, None,
                      {'build': (time.perf_counter() - t0) * 1000.0})


# ---------------------------------------------------------------------------
# groundobs verb（ground_observation の再利用。レンダ済み画像を返す特殊経路）
# ---------------------------------------------------------------------------
# groundobs の出力は「Mitsuba レンダ → センサ格子再標本化 → PSF → ノイズ →
# ストレッチ」の後処理込みなので、scene_dict を返す通常経路には乗らない。
# render_observation_frame() の結果画像を FrameBuild.image01 で返し、
# worker 側のレンダをスキップさせる。カメラは観測幾何から決まるため
# ビューポートのカメラ操作は対象外（フロント側で無効化している）。
_GO_META: Optional[Dict[str, Tuple[Any, Any, Any]]] = None
_GO_CTX_CACHE: Dict[str, tuple] = {}
_GO_CTX_ORDER: List[str] = []
_GO_LOCK = threading.Lock()
_GO_CTX_MAX = 4


def groundobs_mod():
    """ground_observation を遅延 import して返す。"""
    with _MOD_LOCK:
        mod = _MODS.get('groundobs')
        if mod is None:
            import ground_observation as GO  # noqa: N806
            _MODS['groundobs'] = mod = GO
        return mod


def groundobs_args(fields: Optional[Dict[str, Any]]) -> argparse.Namespace:
    """fields を ground_observation の argparse dest 空間の Namespace にする。"""
    global _GO_META
    GO = groundobs_mod()  # noqa: N806
    parser = GO.build_parser()
    if _GO_META is None:
        _GO_META = _action_meta(parser)
    return apply_fields(parser.parse_args([]), fields, _GO_META)


def groundobs_setup(fields: Optional[Dict[str, Any]]) -> tuple:
    """build_context（tumble 軌跡・TLE 読み込み等の前計算）を fields ハッシュで
    キャッシュする。返り値は (args, ctx)。"""
    key = _fields_key('groundobs', fields)
    with _GO_LOCK:
        hit = _GO_CTX_CACHE.get(key)
    if hit is not None:
        return hit
    GO = groundobs_mod()  # noqa: N806
    args = groundobs_args(fields)
    ctx = GO.build_context(args)
    result = (args, ctx)
    with _GO_LOCK:
        _GO_CTX_CACHE[key] = result
        _GO_CTX_ORDER.append(key)
        while len(_GO_CTX_ORDER) > _GO_CTX_MAX:
            _GO_CTX_CACHE.pop(_GO_CTX_ORDER.pop(0), None)
    return result


def build_groundobs_frame(fields: Dict[str, Any], frame: int,
                          width: int, height: int, samples: int) -> FrameBuild:
    """groundobs: バッチと同一の render_observation_frame() を呼ぶ。

    width / height は無視する（画像サイズはセンサ設定 sensor_px で決まる）。
    samples だけ品質オーバーライドとして渡す。refine 時の spp がフォームの
    samples と同じなら、バッチ出力とピクセル一致する
    （ノイズ乱数は noise_seed + frame で決定的）。
    """
    t0 = time.perf_counter()
    GO = groundobs_mod()  # noqa: N806
    args, ctx = groundobs_setup(fields)
    n_frames = max(1, int(args.frames or 1))
    frame = max(0, min(int(frame), n_frames - 1))
    res = GO.render_observation_frame(args, ctx, frame, samples=samples)
    return FrameBuild({}, n_frames, None, res.time_s, None,
                      {'build': (time.perf_counter() - t0) * 1000.0},
                      image01=res.png01)


# ---------------------------------------------------------------------------
# render 系 verb（satellite_orbit + config_loader の再利用）
# ---------------------------------------------------------------------------
_ORBIT_META: Optional[Dict[str, Tuple[Any, Any, Any]]] = None
_ORBIT_SETUP_CACHE: "Dict[str, Tuple[argparse.Namespace, Any, list, Optional[str]]]" = {}
_ORBIT_SETUP_ORDER: List[str] = []
_ORBIT_LOCK = threading.Lock()
# show_orbit の軌跡は 1 更新ごとに 0..N フレーム分の位置を再計算するので、
# セットアップ単位でインクリメンタルにキャッシュする。
_TRAIL_CACHE: Dict[str, Dict[str, List[np.ndarray]]] = {}
# 軌跡の最大点数。これを超えるフレーム数では間引く。
# ⚠ 間引きが発生したフレームでは、バッチ（全フレーム分を蓄積）の軌跡形状と
#    厳密には一致しないため、画もピクセル一致しない。500 点以下なら一致する。
TRAIL_MAX_POINTS = 500


def orbit_args(fields: Optional[Dict[str, Any]]) -> argparse.Namespace:
    """fields を config_loader の argparse dest 空間の Namespace にする。"""
    global _ORBIT_META
    from config_loader import build_parser as _orbit_parser
    args = _orbit_parser().parse_args([])
    if _ORBIT_META is None:
        _ORBIT_META = _defaults_meta(args)
    return apply_fields(args, fields, _ORBIT_META)


def _fields_key(verb: str, fields: Optional[Dict[str, Any]]) -> str:
    """fields のハッシュ（セットアップキャッシュのキー）。"""
    blob = json.dumps({'verb': verb, 'fields': fields or {}},
                      sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()


def orbit_setup(verb: str, fields: Dict[str, Any]):
    """`verbs._common.resolve_run_inputs` の結果を fields ハッシュでキャッシュする。

    CsvEphemeris の読み込みや NumericalPropagator の構築を毎更新やり直さない
    ためのもの。返り値は (key, args, config, objects, earth_day_texture)。
    """
    key = _fields_key(verb, fields)
    with _ORBIT_LOCK:
        hit = _ORBIT_SETUP_CACHE.get(key)
    if hit is not None:
        return (key,) + hit

    mods = orbit_mod()
    args = orbit_args(fields)
    # validate_model_paths は parser.error() → SystemExit(2) を投げる。
    # ワーカーのレンダースレッドを殺さないよう捕まえて、stderr の文言を
    # そのままエラーメッセージにする。
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            config, objects, earth_day_texture = mods.VC.resolve_run_inputs(args)
    except SystemExit as exc:
        message = err.getvalue().strip().splitlines()
        raise ValueError(message[-1] if message else f'設定の検証に失敗しました ({exc})') from None

    with _ORBIT_LOCK:
        _ORBIT_SETUP_CACHE[key] = (args, config, objects, earth_day_texture)
        _ORBIT_SETUP_ORDER.append(key)
        while len(_ORBIT_SETUP_ORDER) > 8:
            dropped = _ORBIT_SETUP_ORDER.pop(0)
            _ORBIT_SETUP_CACHE.pop(dropped, None)
            _TRAIL_CACHE.pop(dropped, None)  # 軌跡キャッシュも道連れで捨てる
    return key, args, config, objects, earth_day_texture


def orbit_trail_histories(key: str, objects: list, config: Any,
                          start_frame: int, frame: int, total_frames: int,
                          period_s: float) -> Dict[str, List[np.ndarray]]:
    """show_orbit 用の軌跡履歴を frame まで再計算する（バッチ相当）。

    バッチ（verbs/render.py）はフレームを順に描くので、フレーム N の時点で
    trail_histories には start_frame..N の位置が溜まっている。ライブは任意の
    フレームへ飛べるので、要求フレームまでの位置列をここで作り直す。
    セットアップキー単位でインクリメンタルにキャッシュするので、フレームを
    進めるスクラブは 1 フレーム分の追加計算で済む。

    ⚠ TRAIL_MAX_POINTS を超えるフレーム数では等間隔に間引く（最後の点は必ず
      含む）。間引きが起きたフレームの画はバッチ出力とピクセル一致しない。
    """
    mods = orbit_mod()
    SO = mods.SO  # noqa: N806
    with _ORBIT_LOCK:
        cache = _TRAIL_CACHE.setdefault(key, {})
    names = [spec.name.replace(' ', '_') for spec in objects]
    for name in names:
        cache.setdefault(name, [])

    have = len(cache[names[0]]) if names else 0
    want = max(0, frame - start_frame + 1)
    for index in range(have, want):
        f = start_frame + index
        time_s = SO.compute_frame_time(f, total_frames, config, period_s)
        sun_direction = SO.resolve_sun_direction(time_s, config)
        for spec, name in zip(objects, names):
            state = SO.compute_object_state(spec, time_s, sun_direction)
            cache[name].append(state.position_km.copy())

    out: Dict[str, List[np.ndarray]] = {}
    for name in names:
        trail = cache[name][:want]
        if len(trail) > TRAIL_MAX_POINTS:
            idx = np.unique(np.linspace(0, len(trail) - 1, TRAIL_MAX_POINTS).astype(int))
            trail = [trail[i] for i in idx]
        out[name] = trail
    return out


def orbit_earth_textures(tmp_dir: Path, time_s: float, sun_direction: np.ndarray,
                         config: Any) -> Tuple[float, Optional[Path], Optional[Path]]:
    """`satellite_orbit.build_earth_textures` のキャッシュ版。

    式はバッチと同一。違いは出力先（ワーカーの一時ディレクトリ）と、
    (テクスチャ, mtime, マスク解像度, softness, 自転角, 太陽方向) をキーに
    ファイル生成を 1 度で済ませる点だけ。同じキーなら同じ画素になる。
    """
    import scene_earth as SE  # noqa: N806 - 2 回目以降は sys.modules から即返る

    earth_rotation_deg = 0.0
    if config.earth.earth_rotation:
        seconds_per_day = config.earth.earth_rotation_period_hours * 3600.0
        earth_rotation_deg = (time_s / seconds_per_day) * 360.0 * config.earth.earth_rotation_speed

    night_emission_texture: Optional[Path] = None
    cloud_opacity_texture: Optional[Path] = None

    night_path = config.earth.night_texture
    if config.earth.use_night_lights and night_path and Path(night_path).exists():
        mask_w = max(int(config.earth.night_mask_res), 16)
        mask_h = max(int(mask_w // 2), 8)
        sun_dir_body = SE.rotate_vector_z(sun_direction, -earth_rotation_deg)
        key = _hash_key((
            'night', str(night_path), _mtime(night_path), mask_w, mask_h,
            round(float(config.earth.night_terminator_softness), 12),
            tuple(round(float(v), 12) for v in np.asarray(sun_dir_body, dtype=float).reshape(-1)),
        ))
        night_dir = tmp_dir / 'night_textures'
        night_dir.mkdir(parents=True, exist_ok=True)
        night_emission_texture = night_dir / f'night_emission_{key}.png'
        if not night_emission_texture.exists():
            night_mask = SE.generate_night_mask(
                sun_dir_body, width=mask_w, height=mask_h,
                softness=config.earth.night_terminator_softness,
            )
            SE.write_combined_night_texture(night_path, night_mask, night_emission_texture)
        _register_tex_file('night', night_emission_texture)

    cloud_path = config.earth.cloud_texture
    if config.earth.use_clouds and cloud_path and Path(cloud_path).exists():
        if abs(config.earth.cloud_opacity - 1.0) > 1e-6:
            key = _hash_key(('cloud', str(cloud_path), _mtime(cloud_path),
                             round(float(config.earth.cloud_opacity), 12)))
            cloud_dir = tmp_dir / 'cloud_textures'
            cloud_dir.mkdir(parents=True, exist_ok=True)
            cloud_opacity_texture = cloud_dir / f'cloud_opacity_{key}.exr'
            if not cloud_opacity_texture.exists():
                SE.write_cloud_opacity_texture(
                    cloud_path, config.earth.cloud_opacity, cloud_opacity_texture)
            _register_tex_file('cloud', cloud_opacity_texture)
        else:
            cloud_opacity_texture = Path(cloud_path)

    return earth_rotation_deg, night_emission_texture, cloud_opacity_texture


def _mtime(path: Any) -> float:
    try:
        return os.path.getmtime(str(path))
    except OSError:
        return 0.0


# 生成済みテクスチャの世代管理。夜光 PNG は 1 枚 ~0.2 MB だが、雲の EXR は
# 原寸 float なので 100 MB を超える。スライダ操作で無限に溜まらないよう、
# 種別ごとに上限枚数を決めて古いものから消す。
_TEX_FILES: Dict[str, List[Path]] = {}
_TEX_LIMITS = {'night': 64, 'cloud': 3}


def _register_tex_file(kind: str, path: Path) -> None:
    files = _TEX_FILES.setdefault(kind, [])
    if path in files:
        files.remove(path)
    files.append(path)
    while len(files) > _TEX_LIMITS.get(kind, 16):
        old = files.pop(0)
        try:
            old.unlink()
        except OSError:
            pass


def _hash_key(parts: tuple) -> str:
    return hashlib.sha1(repr(parts).encode('utf-8')).hexdigest()[:16]


def build_orbit_frame(verb: str, fields: Dict[str, Any], frame: int,
                      width: int, height: int, samples: int,
                      tmp_dir: Path) -> FrameBuild:
    """render / preview / onboard / lightcurve: render_frame を分解して再構成する。

    satellite_orbit.render_frame と同じ順序・同じ式:
        compute_frame_time → resolve_sun_direction → compute_object_state
        → build_scene_objects → resolve_camera → build_earth_textures
        → scene_builder.create_scene
    トーンマップ（apply_tonemap + Bitmap(srgb_gamma=False)）も render_frame と
    同一経路を通す（FrameBuild.tonemap で指示）。
    """
    timings: Dict[str, float] = {}
    t_setup = time.perf_counter()
    key, args, config, objects, _tex = orbit_setup(verb, fields)
    mods = orbit_mod()
    SO = mods.SO  # noqa: N806
    timings['setup'] = (time.perf_counter() - t_setup) * 1000.0

    t_state = time.perf_counter()
    total_frames = max(1, int(getattr(args, 'frames', 60) or 1))
    start_frame = int(getattr(args, 'start_frame', 0) or 0)
    end_frame = getattr(args, 'end_frame', None)
    last_frame = (int(end_frame) if end_frame is not None else total_frames) - 1
    frame = max(start_frame, min(int(frame), max(start_frame, last_frame)))

    period_s = SO.default_time_span_s(objects[0])
    time_s = SO.compute_frame_time(frame, total_frames, config, period_s)
    sun_direction = SO.resolve_sun_direction(time_s, config)
    states = [SO.compute_object_state(obj, time_s, sun_direction) for obj in objects]

    trail_histories: Optional[Dict[str, List[np.ndarray]]] = None
    if config.lighting.show_orbit:
        trail_histories = orbit_trail_histories(
            key, objects, config, start_frame, frame, total_frames, period_s)
    timings['state'] = (time.perf_counter() - t_state) * 1000.0

    t_obj = time.perf_counter()
    scene_objects = SO.build_scene_objects(states, config, trail_histories)
    camera_position, camera_target, camera_up, fov = SO.resolve_camera(states, config)
    timings['objects'] = (time.perf_counter() - t_obj) * 1000.0

    t_tex = time.perf_counter()
    earth_rotation_deg, night_emission_texture, cloud_opacity_texture = orbit_earth_textures(
        tmp_dir, time_s, sun_direction, config)
    timings['tex'] = (time.perf_counter() - t_tex) * 1000.0

    t_dict = time.perf_counter()
    scene_dict = mods.SB.create_scene(
        camera_position=camera_position,
        camera_target=camera_target,
        camera_up=camera_up,
        satellite_objects=scene_objects,
        sun_direction=sun_direction,
        earth_texture=config.earth.day_texture,
        width=width,
        height=height,
        fov=fov,
        use_advanced_lighting=config.lighting.use_advanced_optics,
        sun_temperature=config.lighting.sun_temperature,
        starfield_brightness=config.lighting.starfield_brightness,
        hdri_path=config.lighting.hdri_path,
        sample_count=samples,
        earth_rotation_deg=earth_rotation_deg,
        earth_night_texture=config.earth.night_texture,
        night_emission_texture=str(night_emission_texture) if night_emission_texture else None,
        earth_cloud_texture=(str(cloud_opacity_texture) if cloud_opacity_texture
                             else config.earth.cloud_texture),
        use_night_lights=config.earth.use_night_lights,
        use_clouds=config.earth.use_clouds,
        cloud_opacity=config.earth.cloud_opacity,
        use_atmosphere=config.earth.use_atmosphere,
        atmosphere_density=config.earth.atmosphere_density,
        atmosphere_radius_scale=config.earth.atmosphere_radius_scale,
    )
    timings['dict'] = (time.perf_counter() - t_dict) * 1000.0

    camera = {
        'origin': _vec(camera_position, [0.0, 0.0, 0.0]),
        'target': _vec(camera_target, [0.0, 0.0, 0.0]),
        'up': _vec(camera_up, [0.0, 0.0, 1.0]),
        'fov': float(fov),
    }
    tonemap = (float(config.lighting.exposure), float(config.lighting.gamma))
    return FrameBuild(scene_dict, total_frames, camera, time_s, tonemap, timings)


def build_frame(verb: str, fields: Dict[str, Any], frame: int,
                width: int, height: int, samples: int, tmp_dir: Path) -> FrameBuild:
    """verb に応じたフレームビルダーへディスパッチする。"""
    if verb == 'relative':
        return build_relative_frame(fields, frame, width, height, samples)
    if verb == 'rotation':
        return build_rotation_frame(fields, frame, width, height, samples)
    if verb == 'groundobs':
        return build_groundobs_frame(fields, frame, width, height, samples)
    if verb in ORBIT_VERBS:
        return build_orbit_frame(verb, fields, frame, width, height, samples, tmp_dir)
    raise ValueError(f'ライブプレビュー未対応 verb: {verb}')


# ---------------------------------------------------------------------------
# インタラクティブ品質（操作中の簡易描画）
# ---------------------------------------------------------------------------
# ビューポートをドラッグしている間だけ絵を単純化して応答性を稼ぐ
# （Blender のビューポート劣化表示に相当）。プロファイルは
#   ① simplify_fields()  : verb ごとに「重い物理」をフィールドレベルで落とす
#   ② simplify_scene()   : シーン辞書の integrator を浅い path に差し替える
#   ③ interactive_resolution() / INTERACTIVE_SPP_MAX : 解像度と spp を落とす
# の 3 つだけ。ここを読めばプレビューの劣化内容がすべて分かる。
#
# ⚠ この経路は refine（アイドル時の高品質再レンダ）では絶対に通さない。
#   refine はバッチ出力とのピクセル一致を保証する経路なので、
#   `_render_once` では is_refine=True のとき simplified=False に固定する。

# 追加の解像度分割率（要求 scale の上にさらに掛ける）
INTERACTIVE_SCALE = 0.5
# 分割後の最低幅 [px]（これを下回るときは分割率を緩める）
INTERACTIVE_MIN_WIDTH = 96
# spp の上限（カメラ高速パスではこの spp のシーンをそのまま使い回す）
INTERACTIVE_SPP_MAX = 1
# 差し替える積分器（大気を切るので volpath は不要）
INTERACTIVE_INTEGRATOR = {'type': 'path', 'max_depth': 2}

# verb 別の fields 上書き。値はそのまま fields のコピーへ setitem される。
#   render 系: 大気（volpath 経路）・雲・夜光（PNG 生成）・starfield envmap を停止。
#              starfield_brightness は据え置き（hdri なしの定数背景の明るさになる）。
#   relative : starfield envmap を停止（env_brightness 由来の定数環境光へ）、
#              max_depth を integrator 差し替えと揃える。show_earth は据え置き
#              （地球球体 1 個は軽く、テクスチャはプレビュー用 2048px 縮小が効く）。
#   rotation : 元々軽いので fields は触らない（integrator 差し替えのみ）。
INTERACTIVE_FIELD_OVERRIDES: Dict[str, Dict[str, Any]] = {
    'orbit': {
        'use_atmosphere': False,
        'use_clouds': False,
        'use_night_lights': False,
        'hdri_path': None,
    },
    'relative': {
        'starfield': None,
        'max_depth': INTERACTIVE_INTEGRATOR['max_depth'],
    },
    'rotation': {},
    # groundobs: PSF 畳み込みコストが supersample² で効くので 1 に落とす
    #（integrator 差し替えと解像度分割は image01 経路では効かない）。
    'groundobs': {'supersample': 1},
}


def simplify_fields(verb: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """インタラクティブ品質用に fields を差し替えたコピーを返す。

    元の dict は変更しない。上書きキーは INTERACTIVE_FIELD_OVERRIDES 参照。
    ここで fields が変わることで `_fields_key`（render 系のセットアップ
    キャッシュ）のハッシュも変わるので、簡易版と通常版は別エントリになる。
    """
    group = 'orbit' if verb in ORBIT_VERBS else verb
    overrides = INTERACTIVE_FIELD_OVERRIDES.get(group)
    if not overrides:
        return dict(fields or {})
    out = dict(fields or {})
    out.update(overrides)
    return out


def simplify_scene(scene_dict: dict) -> dict:
    """シーン辞書の integrator を浅い path トレーサへ差し替えたコピーを返す。"""
    out = dict(scene_dict)
    out['integrator'] = dict(INTERACTIVE_INTEGRATOR)
    return out


def interactive_resolution(width: int, height: int) -> Tuple[int, int]:
    """プレビュー解像度をさらに INTERACTIVE_SCALE 倍する（最低幅でクランプ）。"""
    factor = INTERACTIVE_SCALE
    if width * factor < INTERACTIVE_MIN_WIDTH:
        factor = min(1.0, INTERACTIVE_MIN_WIDTH / max(1, width))
    return (max(1, int(round(width * factor))),
            max(1, int(round(height * factor))))


def frame_resolution(verb: str, fields: Dict[str, Any]) -> Tuple[int, int]:
    """fields から出力解像度（フル品質時）を取り出す。"""
    flat = flatten_fields(fields)
    if verb == 'groundobs':
        # 画像サイズはセンサ設定で決まる（width/height フィールドは無い）
        try:
            px = int(flat.get('sensor_px') or 256)
        except (TypeError, ValueError):
            px = 256
        px = max(MIN_RENDER_PX, px)
        return px, px
    if verb in ORBIT_VERBS:
        default_w, default_h = 1920, 1080
    else:
        default_w, default_h = 640, 480
    try:
        width = int(flat.get('width') or default_w)
    except (TypeError, ValueError):
        width = default_w
    try:
        height = int(flat.get('height') or default_h)
    except (TypeError, ValueError):
        height = default_h
    return max(MIN_RENDER_PX, width), max(MIN_RENDER_PX, height)


# ---------------------------------------------------------------------------
# カメラ高速パス（ロード済みシーンの to_world だけ差し替える）
# ---------------------------------------------------------------------------
# ビューポートのドラッグ（オービット/パン/ドリー）はカメラしか変えないのに、
# 従来はフィールドが 1 つでも変われば毎回 mi.load_dict していた。実測で
# 簡易描画 ~150 ms のうち ~140 ms が load_dict（メッシュ再ロード + BVH 再構築）
# なので、解像度や spp をこれ以上落としても速くならない。
#
# そこで interactive 更新に限り、直前に読み込んだシーンを 1 エントリだけ保持し、
# 「カメラ 4 項目以外が完全に同じ」なら `mi.traverse(scene)['camera.to_world']`
# を look_at で差し替えて `params.update()` → 再レンダする。
#
# ⚠ 高速パスとリビルドパスの画は**ビット一致していなければならない**。そのため
#   camera_from_fields() が返す (origin, target, up) は、各 verb の通常ビルドが
#   look_at に渡す値と厳密に一致させること:
#     relative : args.camera_origin / camera_target / camera_up
#                （argparse 既定の up=[0,1,0] は build_scene の既定と一致）
#     rotation : simple_rotation.build_scene が up=[0,1,0] 固定
#     render 系: resolve_camera の末尾で camera.overrides が origin/target/up/fov
#                を丸ごと上書きするので、3 つとも fields にあるときだけ一致する
#                （無いときは view_mode 由来＝状態依存なので高速パス不可）
CAMERA_FIELDS = ('camera_origin', 'camera_target', 'camera_up', 'camera_fov')


def _vec3(value: Any) -> Optional[List[float]]:
    """3 要素の float リストへ落とす（数値化できなければ None）。"""
    if value is None:
        return None
    try:
        arr = np.asarray(value, dtype=float).reshape(-1)
    except (TypeError, ValueError):
        return None
    if arr.size < 3 or not np.all(np.isfinite(arr[:3])):
        return None
    return [float(v) for v in arr[:3]]


def camera_from_fields(verb: str, fields: Dict[str, Any]
                       ) -> Optional[Tuple[List[float], List[float], List[float]]]:
    """高速パス用に fields からカメラ (origin, target, up) を取り出す。

    高速パスの対象外なら None を返す（呼び出し側はリビルドへ落ちる）。
    """
    flat = flatten_fields(fields)
    origin = _vec3(flat.get('camera_origin'))
    target = _vec3(flat.get('camera_target'))
    if origin is None or target is None:
        # フロントはドラッグ開始時にオーバーライド欄へ実カメラをシードするので、
        # 両方揃っていない = まだ操作していない or 非対応設定。
        return None
    if verb == 'rotation':
        # simple_rotation.build_scene は up=[0,1,0] 固定（--camera-up は無い）
        return origin, target, [0.0, 1.0, 0.0]
    up = _vec3(flat.get('camera_up'))
    if up is None:
        if verb in ORBIT_VERBS:
            return None      # view_mode 由来の up は状態依存 → 一致保証ができない
        up = [0.0, 1.0, 0.0]  # relative の argparse 既定 = build_scene の既定
    return origin, target, up


def camera_fov_field(fields: Dict[str, Any]) -> Optional[float]:
    """fields の camera_fov（生値）。高速パスの可否判定に使う。

    fov が変わるとフィルム側の投影行列だけでなく orbit 系の resolve_camera の
    分岐にも効くので、変わっていたら高速パスを諦めてリビルドする
    （ドラッグ中に fov は変わらないので実害はない）。
    """
    raw = flatten_fields(fields).get('camera_fov')
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def camera_cache_key(verb: str, fields: Dict[str, Any], frame: int,
                     width: int, height: int, samples: int) -> str:
    """カメラ 4 項目を除いた「シーンの中身」のハッシュ。

    ここが一致していれば、シーン中でカメラ以外は完全に同一なので to_world の
    差し替えだけで正しい画が得られる。fields は simplify_fields 後のものを渡す。
    """
    flat = flatten_fields(fields)
    rest = {k: v for k, v in flat.items() if k not in CAMERA_FIELDS}
    blob = json.dumps({'verb': verb, 'frame': int(frame),
                       'w': int(width), 'h': int(height), 'spp': int(samples),
                       'fields': rest},
                      sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# ワーカー本体
# ---------------------------------------------------------------------------
DEFAULT_QUALITY = {'scale': 0.5, 'spp': 8, 'refine_spp': 64, 'refine': True,
                   'interactive': False}


def normalize_quality(q: Optional[dict]) -> dict:
    """quality 辞書を安全な範囲に丸める。"""
    q = dict(DEFAULT_QUALITY, **(q or {}))
    try:
        scale = float(q.get('scale', 0.5))
    except (TypeError, ValueError):
        scale = 0.5
    scale = min(1.0, max(0.1, scale))
    def _int(key, fallback, lo, hi):
        try:
            v = int(q.get(key, fallback))
        except (TypeError, ValueError):
            v = fallback
        return min(hi, max(lo, v))
    interactive = q.get('interactive', False)
    if isinstance(interactive, str):
        interactive = interactive.strip().lower() not in ('', '0', 'false', 'no', 'off')
    return {
        'scale': scale,
        'spp': _int('spp', 8, 1, 4096),
        'refine_spp': _int('refine_spp', 64, 1, 8192),
        'refine': bool(q.get('refine', True)),
        # 操作中の簡易描画（既定 false = 従来クライアント互換）
        'interactive': bool(interactive),
    }


class LiveWorker:
    """desired state を受け取り、最新の 1 枚だけを描き続けるレンダーループ。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop = False

        self.desired: Optional[dict] = None
        self.gen_requested = 0
        self.gen_rendered = -1
        self.rendering = False
        self.refine_pending = False

        self.png: Optional[bytes] = None
        self.png_refined = False
        # 直近フレームが簡易化描画（interactive 品質）で描かれたか
        self.png_simplified = False
        # 直近レンダがカメラ高速パス（load_dict を省いた to_world 差し替え）だったか
        self.png_fast_path = False
        # インタラクティブシーンキャッシュ（1 エントリ）。レンダースレッド専用。
        #   {'key', 'fov', 'scene', 'params', 'tonemap', 'traj_frames',
        #    'time_s', 'camera'}
        self._cam_cache: Optional[Dict[str, Any]] = None
        self.last_ms: Optional[float] = None
        self.error: Optional[str] = None
        self.frame = 0
        self.traj_frames = 0
        # 直近レンダで解決されたカメラ（シーン単位）とフレーム物理時刻。
        # フロントはこれを render 系のオービット操作の初期値シードに使う。
        self.camera: Optional[Dict[str, Any]] = None
        self.time_s: Optional[float] = None
        self.verb: Optional[str] = None

        self._tmp_dir = tempfile.mkdtemp(prefix='mrender_live_')
        self._tmp_png = str(Path(self._tmp_dir) / 'live.png')
        self._thread = threading.Thread(target=self._loop, name='live-render', daemon=True)

    # --- 制御 ---
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def cleanup(self) -> None:
        """一時ディレクトリ（地球テクスチャの生成物を含む）を消す。"""
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    # --- API から呼ばれる ---
    def update(self, verb: str, fields: dict, frame: int, quality: Optional[dict]) -> int:
        """desired state を差し替えて generation を進める。"""
        with self._cond:
            self.desired = {
                'verb': verb,
                'fields': fields or {},
                'frame': int(frame or 0),
                'quality': normalize_quality(quality),
            }
            self.gen_requested += 1
            self.refine_pending = False
            self._cond.notify_all()
            return self.gen_requested

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'gen_requested': self.gen_requested,
                'gen_rendered': self.gen_rendered,
                'rendering': self.rendering,
                'refined': self.png_refined,
                'simplified': self.png_simplified,
                'fast_path': self.png_fast_path,
                'last_ms': self.last_ms,
                'error': self.error,
                'frame': self.frame,
                'traj_frames': self.traj_frames,
                'verb': self.verb,
                'camera': self.camera,
                'time_s': self.time_s,
            }

    def image(self) -> Tuple[Optional[bytes], int, bool, bool]:
        with self._lock:
            return self.png, self.gen_rendered, self.png_refined, self.png_simplified

    def status_word(self) -> str:
        with self._lock:
            if self.rendering or self.gen_rendered != self.gen_requested:
                return 'rendering'
            if self.error:
                return 'error'
            return 'idle'

    # --- レンダースレッド ---
    def _has_work(self) -> bool:
        if self.desired is None:
            return False
        if self.gen_rendered != self.gen_requested:
            return True
        return self.refine_pending

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._stop and not self._has_work():
                    self._cond.wait()
                if self._stop:
                    return
                desired = dict(self.desired)
                gen = self.gen_requested
                # 新しい gen があればそちらを優先し、refine は後回し
                is_refine = (self.gen_rendered == gen) and self.refine_pending
                self.rendering = True
            try:
                self._render_once(desired, gen, is_refine)
            except (Exception, SystemExit):  # noqa: BLE001 - ワーカーは絶対に死なせない
                # SystemExit も拾う: config_loader の検証は parser.error() 経由で
                # SystemExit を投げるので、素通しするとレンダースレッドが死ぬ。
                with self._cond:
                    self.error = traceback.format_exc(limit=3).strip()
                    self.gen_rendered = gen
                    self.refine_pending = False
            finally:
                with self._cond:
                    self.rendering = False
                    self._cond.notify_all()

    def _render_once(self, desired: dict, gen: int, is_refine: bool) -> None:
        quality = desired['quality']
        verb = desired.get('verb') or 'relative'
        # 簡易化は「プレビュー かつ interactive 指定」のときだけ。refine は
        # 常にフル品質・簡易化なし（バッチ出力とのピクセル一致を守るため）。
        simplified = bool(quality['interactive']) and not is_refine
        fast_path = False
        try:
            full_w, full_h = frame_resolution(verb, desired['fields'])
            if is_refine:
                width = min(full_w, REFINE_MAX_W)
                height = min(full_h, REFINE_MAX_H)
                samples = quality['refine_spp']
            else:
                width = max(MIN_RENDER_PX, int(full_w * quality['scale']))
                height = max(MIN_RENDER_PX, int(full_h * quality['scale']))
                samples = quality['spp']
                if simplified:
                    width, height = interactive_resolution(width, height)
                    samples = min(samples, INTERACTIVE_SPP_MAX)

            fields = simplify_fields(verb, desired['fields']) if simplified \
                else desired['fields']

            t0 = time.perf_counter()
            stages: Dict[str, float] = {}

            # --- カメラ高速パスの可否判定 ---
            # interactive のときだけ。カメラ 4 項目以外が直前のシーンと同一で、
            # fov も変わっていなければ to_world 差し替えだけで済む。
            cam = camera_from_fields(verb, fields) if simplified else None
            cache_key: Optional[str] = None
            fov_key: Optional[float] = None
            if cam is not None:
                cache_key = camera_cache_key(verb, fields, desired['frame'],
                                             width, height, samples)
                fov_key = camera_fov_field(fields)
            entry = self._cam_cache
            fast_path = (cam is not None and entry is not None
                         and entry['key'] == cache_key and entry['fov'] == fov_key)

            pre_img = None   # groundobs の処理済み画像（リビルド経路でのみ載る）
            if fast_path:
                origin, target, up = cam
                t_cam = time.perf_counter()
                # look_at の引数は各 verb の通常ビルドと同じ 3 つ（上の
                # camera_from_fields の注意書き参照）。ここが一致していれば
                # 生成される to_world も画もリビルドパスとビット一致する。
                entry['params']['camera.to_world'] = mi.ScalarTransform4f.look_at(
                    origin=origin, target=target, up=up)
                entry['params'].update()
                t_render = time.perf_counter()
                image = mi.render(entry['scene'])
                t_write = time.perf_counter()
                tonemap = entry['tonemap']
                traj_frames = entry['traj_frames']
                time_s = entry['time_s']
                camera = dict(entry['camera'] or {})
                camera.update(origin=list(origin), target=list(target), up=list(up))
                stages['cam'] = (t_render - t_cam) * 1000.0
            else:
                build = build_frame(verb, fields, desired['frame'],
                                    width, height, samples, Path(self._tmp_dir))
                stages.update(build.timings)
                pre_img = build.image01
                if pre_img is not None:
                    # groundobs 経路: verb 側で処理済み画像を受け取った。
                    # worker のレンダはスキップ（センサ後処理込みの画のため）。
                    t_pre = t_load = t_render = t_write = time.perf_counter()
                    image = None
                else:
                    # プレビューでは envmap / 大判テクスチャを縮小して応答性を優先。
                    # refine では必ず原寸（バッチ出力とのピクセル一致はここで担保する）。
                    t_pre = time.perf_counter()
                    scene_dict = build.scene_dict
                    if simplified:
                        scene_dict = simplify_scene(scene_dict)
                    scene_dict = preload_bitmaps(
                        scene_dict,
                        0 if is_refine else PREVIEW_ENV_MAX_WIDTH,
                        0 if is_refine else PREVIEW_TEX_MAX_WIDTH)
                    t_load = time.perf_counter()
                    scene = mi.load_dict(scene_dict)
                    t_render = time.perf_counter()
                    image = mi.render(scene)
                    t_write = time.perf_counter()
                tonemap = build.tonemap
                traj_frames = build.traj_frames
                time_s = build.time_s
                camera = build.camera
                stages['bitmap'] = (t_load - t_pre) * 1000.0
                stages['load_dict'] = (t_render - t_load) * 1000.0
                if simplified:
                    # インタラクティブシーンキャッシュを 1 エントリだけ更新
                    # （verb 切替・fields 変更で key が変わると自然に捨てられる）。
                    self._cam_cache = None if cam is None else {
                        'key': cache_key,
                        'fov': fov_key,
                        'scene': scene,
                        'params': mi.traverse(scene),
                        'tonemap': build.tonemap,
                        'traj_frames': build.traj_frames,
                        'time_s': build.time_s,
                        'camera': build.camera,
                    }

            if pre_img is not None:
                # groundobs: [0,1] ストレッチ済み画像をそのまま 8bit 化
                #（ground_observation.save_png と同一経路 = バッチとピクセル一致）。
                bmp = mi.Bitmap(np.ascontiguousarray(
                    pre_img.astype(np.float32))).convert(
                    mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8,
                    srgb_gamma=False)
                bmp.write(self._tmp_png)
            elif tonemap is not None:
                # render 系: satellite_orbit.render_frame と同一のトーンマップ経路。
                # apply_tonemap の出力は既にガンマ済みなので srgb_gamma=False で
                # そのまま 8bit 量子化する（二重ガンマの防止）。
                exposure, gamma = tonemap
                tonemapped = orbit_mod().SB.apply_tonemap(image, exposure=exposure, gamma=gamma)
                bmp = mi.Bitmap(tonemapped).convert(
                    mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8, srgb_gamma=False)
                bmp.write(self._tmp_png)
            else:
                # relative / rotation: write_async=False は必須（既定の非同期
                # 書き出しだと read_bytes がファイル生成前に走る）。バッチ側と
                # 同じ convert_to_bitmap(uint8 sRGB) 経路なので画は一致する。
                mi.util.write_bitmap(self._tmp_png, image, write_async=False)
            png = Path(self._tmp_png).read_bytes()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            stages['render'] = (t_write - t_render) * 1000.0
            stages['write'] = (time.perf_counter() - t_write) * 1000.0
            print('live: verb={}{}{} frame={} {}x{} spp={} {} total={:.0f}ms'.format(
                verb, ' [interactive]' if simplified else '',
                ' [fast-cam]' if fast_path else '',
                desired['frame'], width, height, samples,
                ' '.join(f'{k}={v:.0f}ms' for k, v in stages.items()), elapsed_ms),
                flush=True)
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            with self._cond:
                self.error = f'{type(exc).__name__}: {exc}'
                self.gen_rendered = gen
                self.refine_pending = False
            return

        with self._cond:
            # レンダ中に新しい update が来ていたら、この結果は捨てて次へ
            superseded = (gen != self.gen_requested)
            if not superseded:
                self.png = png
                self.png_refined = is_refine
                self.png_simplified = simplified
                self.png_fast_path = fast_path
                self.gen_rendered = gen
                self.frame = desired['frame']
                self.traj_frames = traj_frames
                self.camera = camera
                self.time_s = time_s
                self.verb = verb
                self.error = None
                # プレビュー品質だったら、アイドル時にフル解像度で描き直す
                preview_is_full = (
                    not is_refine
                    and width >= min(full_w, REFINE_MAX_W)
                    and height >= min(full_h, REFINE_MAX_H)
                    and samples >= quality['refine_spp']
                )
                if is_refine or preview_is_full:
                    self.png_refined = True
                    self.refine_pending = False
                else:
                    self.refine_pending = bool(quality['refine'])
            self.last_ms = elapsed_ms
            self._cond.notify_all()


WORKER = LiveWorker()
_SERVER: Optional[ThreadingHTTPServer] = None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class LiveHandler(BaseHTTPRequestHandler):
    """ライブプレビュー API（JSON + PNG）。"""

    server_version = 'mrenderLive/1.0'
    protocol_version = 'HTTP/1.1'

    # --- ヘルパ ---
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _err(self, message: str, status: int = 400) -> None:
        self._send_json({'error': message}, status=status)

    def _read_json(self) -> Any:
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8') or '{}')

    def _live_headers(self, gen: int, refined: bool, simplified: bool = False) -> None:
        snap = WORKER.snapshot()
        self.send_header('X-Live-Gen', str(gen))
        self.send_header('X-Live-Refined', '1' if refined else '0')
        # そのフレームが操作中の簡易化描画かどうか（フロントの品質バッジ用）
        self.send_header('X-Live-Simplified', '1' if simplified else '0')
        self.send_header('X-Live-Status', WORKER.status_word())
        self.send_header('X-Live-Ms', str(int(snap['last_ms'] or 0)))
        if snap['error']:
            self.send_header('X-Live-Error', urllib.parse.quote(str(snap['error'])))
        self.send_header('Cache-Control', 'no-store')

    def log_message(self, fmt, *args):  # noqa: D102 - 静かなログ
        pass

    # --- GET ---
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        route = parsed.path

        if route == '/health':
            self._send_json({'ok': True, 'pid': os.getpid()})
        elif route == '/status':
            self._send_json(WORKER.snapshot())
        elif route == '/frame':
            self._serve_frame(qs)
        else:
            self._err('not found', 404)

    def _serve_frame(self, qs: Dict[str, List[str]]) -> None:
        png, gen, refined, simplified = WORKER.image()
        if png is None:
            self.send_response(404)
            self._live_headers(gen, refined, simplified)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            body = json.dumps({'error': 'no frame yet'}).encode('utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            known_gen = int((qs.get('known_gen') or ['-1'])[0])
        except ValueError:
            known_gen = -1
        known_refined_raw = (qs.get('known_refined') or ['0'])[0]
        known_refined = str(known_refined_raw).strip().lower() in ('1', 'true', 'yes')

        if known_gen == gen and known_refined == refined:
            self.send_response(304)
            self._live_headers(gen, refined, simplified)
            self.end_headers()
            return

        self.send_response(200)
        self._live_headers(gen, refined, simplified)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(png)))
        self.end_headers()
        self.wfile.write(png)

    # --- POST ---
    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError) as exc:
            self._err(f'JSON パースエラー: {exc}')
            return
        if not isinstance(payload, dict):
            payload = {}

        if route == '/update':
            verb = payload.get('verb', 'relative')
            if verb not in SUPPORTED_VERBS:
                self._err(f'ライブプレビュー未対応 verb: {verb}'
                          f'（対応: {", ".join(SUPPORTED_VERBS)}）', 400)
                return
            gen = WORKER.update(verb, payload.get('fields') or {},
                                payload.get('frame') or 0, payload.get('quality'))
            self._send_json({'ok': True, 'gen': gen})
        elif route == '/shutdown':
            self._send_json({'ok': True})
            WORKER.stop()
            threading.Thread(target=_shutdown_server, daemon=True).start()
        else:
            self._err('not found', 404)


def _shutdown_server() -> None:
    time.sleep(0.2)
    if _SERVER is not None:
        _SERVER.shutdown()
    WORKER.cleanup()
    os._exit(0)


class _ReuseAddrServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description='mrender ライブプレビュー ワーカー')
    parser.add_argument('--port', type=int, default=8601)
    parser.add_argument('--host', type=str, default='127.0.0.1')
    args = parser.parse_args(argv)

    global _SERVER
    _SERVER = _ReuseAddrServer((args.host, args.port), LiveHandler)
    WORKER.start()
    print(f'live_worker: http://{args.host}:{args.port}/  (variant={mi.variant()}, cwd={os.getcwd()})',
          flush=True)
    try:
        _SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WORKER.stop()
        WORKER.cleanup()
        _SERVER.server_close()


if __name__ == '__main__':
    main()
