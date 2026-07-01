"""verb 間で共有する CLI 解決＋セットアップ処理。

`render` / `lightcurve` / `preview` / `onboard` は同じ argparse パーサ
（`config_loader.build_parser`）と同じシーン構築ロジックを使う。重複を避けるため、
ここに「YAML を argparse 既定にマージして引数を解決する」「RenderConfig と物体
仕様を組み立てる」「ffmpeg を呼んで連番→mp4 に変換する」の 3 点を集約している。
`rotation` / `relative` は別パーサを持つので、ここの `resolve_args` は使わず
ffmpeg 動画化（`maybe_make_video`）のみを共有する。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from config_loader import (
    build_parser,
    build_render_config,
    load_yaml_config,
    resolve_earth_day_texture,
    resolve_object_specs,
    validate_model_paths,
)
from render_config import ObjectSpec, RenderConfig


def resolve_args(extra: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """`--config` を解決し、最終的な argparse 名前空間を返す。

    優先順位は CLI 引数 > YAML > argparse 既定値。実装は 2 段階パース:
    1) `parse_known_args` で `--config` の値だけ拾う
    2) その YAML を `set_defaults` で既定値として注入し、再度 `parse_args`
    こうすると CLI の値は YAML より強くなり、未指定キーだけが YAML から入る。
    """
    parser = build_parser()
    args_pre, _ = parser.parse_known_args(extra)
    if args_pre.config:
        parser.set_defaults(**load_yaml_config(args_pre.config))
    args = parser.parse_args(extra)
    return args


def resolve_run_inputs(args: argparse.Namespace):
    """RenderConfig / 物体仕様 / 地球テクスチャを一括で組み立てる。

    Returns:
        `(config, objects, earth_day_texture)` のタプル。
        - `config`: Mitsuba シーン全体に効くレンダリング設定（解像度・サンプル数等）
        - `objects`: シーンに置く物体リスト（衛星/デブリ等の `ObjectSpec`）
        - `earth_day_texture`: 地球昼面テクスチャの解決済みパス
    """
    earth_day_texture = resolve_earth_day_texture(args)
    config: RenderConfig = build_render_config(args, earth_day_texture)
    objects: List[ObjectSpec] = resolve_object_specs(args)
    # 物体に紐づく外部 OBJ/PLY/GLB の存在チェックはここで一括で行う。
    parser = build_parser()
    validate_model_paths(parser, objects)
    return config, objects, earth_day_texture


def frame_range(args: argparse.Namespace):
    """`(start_frame, end_frame)` を計算する。`end_frame` 未指定時は `frames` を使う。

    返り値はいずれも 0 始まりの int。`range(start, end)` でそのまま使える半開区間。
    """
    start_frame = int(getattr(args, 'start_frame', 0))
    end_frame = getattr(args, 'end_frame', None)
    if end_frame is None:
        end_frame = int(args.frames)
    return start_frame, int(end_frame)


def maybe_make_video(frames_dir: Path, *, fps: float = 30.0,
                     output_name: str = 'output.mp4',
                     enabled: bool = False) -> Optional[Path]:
    """`enabled` が True で frames_dir に `frame_*.png` があれば ffmpeg で動画を作る。

    生成パスは `frames_dir.parent / output_name`（= ランディレクトリ直下）。
    H.264 + yuv420p で出すので一般的なプレーヤで再生できる。

    Returns:
        成功時は出力 mp4 のパス。ffmpeg 不在 / フレーム不在 / 失敗時はいずれも
        `None` を返す（例外は投げず、警告だけ stderr に出す）。
    """
    if not enabled:
        return None
    if shutil.which('ffmpeg') is None:
        print('⚠ make-video: ffmpeg が PATH にありません。スキップします。', file=sys.stderr)
        return None
    # ffmpeg の `-i` に渡す連番パターン（frame_0000.png, frame_0001.png, ...）。
    pattern = frames_dir / 'frame_%04d.png'
    sample = next(frames_dir.glob('frame_*.png'), None)
    if sample is None:
        print(f'⚠ make-video: {frames_dir} にフレームが見つかりません。スキップ。', file=sys.stderr)
        return None
    output_path = frames_dir.parent / output_name
    cmd = [
        'ffmpeg', '-y', '-framerate', str(fps),
        '-i', str(pattern),
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        str(output_path),
    ]
    print(f"  ffmpeg: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        # ffmpeg の stderr 先頭 200 文字だけ拾ってデバッグ用に出す。
        print(f'⚠ make-video: ffmpeg 失敗 ({exc.returncode})。stderr: {exc.stderr.decode()[:200]}',
              file=sys.stderr)
        return None
    print(f'  動画: {output_path}')
    return output_path
