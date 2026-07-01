"""`mrender preview` 動詞: 1 フレームだけ即レンダする調整用パイプライン。

連番を回さずに `start_frame` で指定した 1 枚だけ描いて止まる。マテリアル・
ライティング・カメラ位置などを試行錯誤するときの高速イテレーション用。
本体は `render` verb と同じ `satellite_orbit.render_frame` を 1 回呼ぶだけ。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Dict, List

import numpy as np

from runs import RunDir, create_run_dir
from verbs._common import resolve_args, resolve_run_inputs


def run(args: argparse.Namespace) -> RunDir:
    """`start_frame` 1 枚だけをレンダする。マテリアル/ライティング調整向け。

    `--start-frame N` で連番のうち N 番目だけが欲しい、という使い方を想定している。
    `no_frames` / `light_curve` は強制 False（プレビューは PNG を出すのが目的）。
    """
    from satellite_orbit import print_run_summary, render_frame

    # preview の意味づけ上、これらのフラグは強制的に上書きする。
    args.no_frames = False
    args.light_curve = False

    config, objects, earth_day_texture = resolve_run_inputs(args)
    start_frame = int(getattr(args, 'start_frame', 0))
    end_frame = start_frame + 1
    args.start_frame = start_frame
    args.end_frame = end_frame

    primary_name = getattr(args, 'primary_name', None) or objects[0].name
    run_dir = create_run_dir('preview', args, name_hint=primary_name)
    args.output_dir = str(run_dir.path)
    frames_dir = run_dir.frames_dir

    print_run_summary(args, objects, config, run_dir.path, earth_day_texture, start_frame, end_frame)

    status = 'ok'
    extra: Dict[str, object] = {}
    try:
        # 1 フレーム限定なので trail 履歴は空辞書を渡すだけで足りる。
        trail_histories: Dict[str, List[np.ndarray]] = {}
        render_frame(start_frame, args.frames, objects, config, frames_dir, trail_histories)
        extra['frames_dir'] = str(frames_dir)
        extra['frame_count'] = 1
    except Exception as exc:
        status = f'error: {exc!r}'
        raise
    finally:
        run_dir.write_manifest(args, finished_at=datetime.now(), status=status, extra=extra)

    print(f"\nプレビュー保存: {frames_dir}/frame_{start_frame:04d}.png")
    print(f'ランディレクトリ: {run_dir.path}')
    return run_dir


def main(argv=None) -> None:
    """CLI エントリポイント。`mrender preview ...` から呼ばれる。"""
    args = resolve_args(argv)
    run(args)


if __name__ == '__main__':
    main()
