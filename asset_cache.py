"""アセット（画像・変換済みメッシュ）のプロセス内キャッシュ。

バッチレンダリング（satellite_orbit の render_frame / render_light_curve）も
ライブプレビュー（live_worker）も、フレームごとに Mitsuba シーン辞書を作り直して
`mi.load_dict()` に渡す。素朴に書くと以下が毎フレーム走る:

    - `.glb/.gltf` → `.obj` の trimesh 変換（models/iss.glb で実測 0.9 s/回、
      しかも 17 MB の OBJ を毎回上書きする）
    - シーン辞書中の `{'type': 'bitmap'|'envmap', 'filename': ...}` の画像デコード
      （earth_day 21600x10800 JPEG + starfield 8192x4096 EXR で実測 7 s/フレーム級）

どちらもフレーム間で内容が変わらないので、本モジュールで一度だけ実行して
結果を使い回す。元々は live_worker.py 内のプレビュー高速化として実装されていた
ものを、バッチ経路と共有できるよう切り出したもの。

責務:
    - `glb_to_obj_cached()` : GLB/glTF → OBJ 変換の (path, mtime) メモ化
    - `load_bitmap()`       : `mi.Bitmap` の (path, mtime, max_width) メモ化
    - `preload_bitmaps()`   : シーン辞書の `filename` を `bitmap` 実体へ差し替え

⚠ 共有してよいのは **Bitmap まで**。
   読み込み済みの *Emitter / Shape などのシーンオブジェクト* をシーン間で
   使い回してはならない。1 つの emitter が複数シーンに属すると内部状態
   （サンプリング分布など）が壊れ、実測で mean|diff| がリファレンスの ~27%
   ずれた。Bitmap は Mitsuba 側がテクスチャ構築時にコピーするので安全。
"""

from __future__ import annotations

import os
import json
import math
import struct
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Tuple

import mitsuba as mi

# ---------------------------------------------------------------------------
# GLB/glTF → OBJ 変換のキャッシュ
# ---------------------------------------------------------------------------
# Mitsuba 3 は OBJ/PLY の組み込みパーサしか持たないため、glTF は一度 OBJ に
# 落とす必要がある。変換自体は決定的なので (path, mtime) でメモ化できる。
_GLB_CACHE: Dict[Tuple[str, float], str] = {}
_GLB_LOCK = threading.Lock()


def _obj_has_extent(path: Path) -> bool:
    """Reject empty/collapsed exports before reusing a disk cache."""
    first = None
    try:
        with path.open() as fh:
            for line in fh:
                if not line.startswith('v '):
                    continue
                point = tuple(float(v) for v in line.split()[1:4])
                if len(point) != 3 or not all(math.isfinite(v) for v in point):
                    return False
                if first is None:
                    first = point
                elif point != first:
                    return True
    except (OSError, ValueError):
        return False
    return False


def _uses_draco(path: Path) -> bool:
    if path.suffix.lower() == '.glb':
        with path.open('rb') as fh:
            fh.read(12)
            size, kind = struct.unpack('<II', fh.read(8))
            if kind != 0x4E4F534A:
                raise ValueError(f'GLB JSON chunk is missing: {path}')
            doc = json.loads(fh.read(size))
    else:
        doc = json.loads(path.read_text())
    return 'KHR_draco_mesh_compression' in doc.get('extensionsUsed', [])


def _export_draco_with_blender(path: Path, output: Path) -> None:
    blender = shutil.which('blender')
    mac_blender = Path('/Applications/Blender.app/Contents/MacOS/Blender')
    if not blender and mac_blender.is_file():
        blender = str(mac_blender)
    if not blender:
        raise ValueError('Draco圧縮モデルの変換にはBlenderが必要です。展開済みOBJを指定することもできます。')
    helper = Path(__file__).parent / 'tools/convert_gltf_blender.py'
    result = subprocess.run([blender, '--background', '--factory-startup',
                             '--python-exit-code', '1', '--python', str(helper),
                             '--', str(path.resolve()), str(output.resolve())],
                            capture_output=True, text=True, timeout=180)
    if result.returncode or not _obj_has_extent(output):
        raise ValueError(f'Dracoモデルの変換に失敗しました: {path}\n{(result.stdout + result.stderr)[-1000:]}')


def glb_to_obj_cached(path: Path) -> Path:
    """GLB/glTF を OBJ へ変換して返す（既存の新しい OBJ があれば再利用）。

    通常の glTF はシーングラフの変換を適用して結合する。Draco圧縮は
    Blenderで展開する。不正なゼロサイズOBJは既存キャッシュとして採用しない。
    法線は出力せず、Mitsuba側で面から生成する。

    Args:
        path: `.glb` / `.gltf` ファイルへのパス。

    Returns:
        変換先 `.obj` のパス（同一ディレクトリ、拡張子だけ差し替え）。
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
    # プロセスをまたいだ再利用: 変換済み OBJ が GLB より新しければそのまま使う。
    if not (obj_path.exists() and os.path.getmtime(obj_path) >= mtime and _obj_has_extent(obj_path)):
        if _uses_draco(path):
            _export_draco_with_blender(path, obj_path)
        else:
            import trimesh
            scene_or_mesh = trimesh.load(str(path))
            if isinstance(scene_or_mesh, trimesh.Scene):
                meshes = list(scene_or_mesh.dump())
                if not meshes:
                    raise ValueError(f'GLBファイルにメッシュが含まれていません: {path}')
                mesh = trimesh.util.concatenate(meshes)
            else:
                mesh = scene_or_mesh
            mesh.export(str(obj_path), file_type='obj', include_normals=False)
        print(f'  [model conversion] {path} → {obj_path}')

    with _GLB_LOCK:
        _GLB_CACHE[key] = str(obj_path)
    return obj_path


# ---------------------------------------------------------------------------
# ビットマップのプリロード（毎回の mi.load_dict でのデコードを避ける）
# ---------------------------------------------------------------------------
# シーン辞書中の {'type': 'bitmap'|'envmap', 'filename': ...} を
# {'type': ..., 'bitmap': <mi.Bitmap>} に差し替える。Mitsuba 側の処理は
# 同一経路なので画はビット単位でほぼ一致する（実測 max|diff| = 3.6e-7 = float32 誤差）。
_BITMAP_CACHE: Dict[Tuple[str, float, int], Any] = {}
_BITMAP_LOCK = threading.Lock()

# これを超える画素数のビットマップはキャッシュしない（メモリ肥大の防止）。
# バッチ経路では毎フレーム同じ数枚のテクスチャを使い回すため、原寸の
# assets/textures/earth_day.jpg (21600x10800 = 233 Mpx) まで載る値にしてある。
BITMAP_CACHE_MAX_PIXELS = 256_000_000
# キャッシュの最大エントリ数（超えたら挿入順で最古を捨てる）。
BITMAP_CACHE_MAX_ENTRIES = 24

# プレビュー品質で使う envmap の最大幅。assets/starfield.exr は 8192x4096 で、
# envmap プラグインのサンプリング分布構築だけで ~840 ms かかる。プレビューでは
# 縮小して応答性を優先し、バッチ／refine パスでは原寸（0）を使う。
PREVIEW_ENV_MAX_WIDTH = 2048
# プレビュー品質で使う通常テクスチャ（地球昼/夜/雲）の最大幅。
# ⚠ バッチ／refine パスでは必ず 0（原寸）を渡すこと。本番出力とのピクセル一致は
#    原寸でのみ保証される。
PREVIEW_TEX_MAX_WIDTH = 2048


def load_bitmap(path: str, max_width: int = 0):
    """画像を `mi.Bitmap` として読み込む（(path, mtime, max_width) でキャッシュ）。

    Args:
        path: 画像ファイルパス。
        max_width: 0 なら原寸。正値ならその幅を超える画像だけ縦横比維持で縮小する。

    Returns:
        `mi.Bitmap`。呼び出し側で書き換えないこと（キャッシュ実体を共有するため）。
    """
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
            if len(_BITMAP_CACHE) > BITMAP_CACHE_MAX_ENTRIES:
                _BITMAP_CACHE.pop(next(iter(_BITMAP_CACHE)))
    return bmp


def preload_bitmaps(node: Any, env_max_width: int = 0, tex_max_width: int = 0) -> Any:
    """シーン辞書を再帰的に走査し、filename 指定の画像を Bitmap 実体に差し替える。

    `env_max_width` は `'envmap'`、`tex_max_width` は `'bitmap'` に対する幅の
    上限（0 = 原寸）。バッチ経路は両方 0 で呼ぶので画は変わらない。

    Args:
        node: シーン辞書（または再帰中の部分木）。
        env_max_width: envmap の最大幅 [px]（0 = 原寸）。
        tex_max_width: bitmap テクスチャの最大幅 [px]（0 = 原寸）。

    Returns:
        `filename` を `bitmap` に置換した新しい辞書（入力は破壊しない）。
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
# BitmapTexture 実体の共有（BSDF テクスチャ限定）
# ---------------------------------------------------------------------------
# 「ロード済みシーンオブジェクトの共有禁止」規約は Emitter の共有で画が壊れた
# 経験則によるもの。BSDF の reflectance 等に使う BitmapTexture（Texture
# プラグイン実体）は per-scene 状態を持たず、実測でシーン間共有しても
# 出力がビット一致する（ゴールデン検証済み）。一方、Mitsuba の
# BitmapTexture 構築は巨大テクスチャで支配的コストになる
# （assets/textures/earth_day.jpg 233 Mpx で ~6.4 s/シーン → 共有で ~5 ms）。
# そこで share_bsdf_textures() は emitter 系サブツリーを除外した上で
# bitmap テクスチャノードだけを共有 Texture 実体に置き換える。
# Emitter（envmap / area / directional / constant / night_lights の
# radiance）は引き続き共有禁止（Bitmap 実体の共有までは安全）。
_TEXTURE_CACHE: Dict[Tuple[str, float, int], Any] = {}
_TEXTURE_LOCK = threading.Lock()
TEXTURE_CACHE_MAX_ENTRIES = 16

_EMITTER_TYPES = ('envmap', 'area', 'directional', 'constant', 'point', 'spot')
_EMITTER_KEYS = ('emitter', 'radiance', 'irradiance')


def load_texture(path: str, max_width: int = 0):
    """ロード済み BitmapTexture 実体を (path, mtime, max_width) で共有取得する。"""
    mtime = os.path.getmtime(path)
    key = (str(path), mtime, int(max_width))
    with _TEXTURE_LOCK:
        hit = _TEXTURE_CACHE.get(key)
    if hit is not None:
        return hit
    tex = mi.load_dict({'type': 'bitmap', 'bitmap': load_bitmap(path, max_width)})
    with _TEXTURE_LOCK:
        _TEXTURE_CACHE[key] = tex
        if len(_TEXTURE_CACHE) > TEXTURE_CACHE_MAX_ENTRIES:
            _TEXTURE_CACHE.pop(next(iter(_TEXTURE_CACHE)))
    return tex


def share_bsdf_textures(node: Any, tex_max_width: int = 0, _in_emitter: bool = False) -> Any:
    """シーン辞書の BSDF 用 bitmap テクスチャを共有 Texture 実体に置き換える。

    emitter 系サブツリー（type が emitter プラグイン、または radiance /
    irradiance / emitter キー配下）は置換しない。preload_bitmaps() より
    先に呼ぶこと（こちらは filename ノードを丸ごと Texture 実体にする）。
    """
    if isinstance(node, dict):
        ntype = node.get('type')
        in_emitter = _in_emitter or ntype in _EMITTER_TYPES
        filename = node.get('filename')
        if (not in_emitter and ntype == 'bitmap' and isinstance(filename, str)
                and len(node) <= 2):   # filename 以外のパラメータ付きノードは対象外
            try:
                return load_texture(filename, tex_max_width)
            except Exception:  # noqa: BLE001 - 読めなければそのまま
                return node
        return {k: (v if k in _EMITTER_KEYS and not isinstance(v, dict) else
                    share_bsdf_textures(v, tex_max_width,
                                        in_emitter or k in _EMITTER_KEYS))
                for k, v in node.items()}
    if isinstance(node, list):
        return [share_bsdf_textures(v, tex_max_width, _in_emitter) for v in node]
    return node
