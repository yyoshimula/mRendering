"""`mrender render` 動詞: フレーム連番をレンダリングする。

実体のレンダリング処理（軌道計算→Mitsuba シーン構築→`mi.render()`）は
`satellite_orbit.render_frame` が持っており、本ファイルは:
1) 引数解決 → `RenderConfig` 構築
2) 出力先となる `RunDir` を準備
3) フレームループを回す
4) 終了時に manifest を書き、必要なら ffmpeg で mp4 化
というオーケストレーションのみを担う。`onboard` verb もここの `run()` を再利用する。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np

from runs import RunDir, create_run_dir
from verbs._common import frame_range, maybe_make_video, resolve_args, resolve_run_inputs


def run(args: argparse.Namespace, run_dir: RunDir | None = None) -> RunDir:
    """frames を `runs/.../frames/` 以下へ書き出す。

    Args:
        args: 解決済み引数（`resolve_args` 経由）。`frames`, `start_frame`,
            `end_frame`, `make_video`, `no_frames` などを参照。
        run_dir: 既存ランへ追記する場合に渡す。`None` なら新規作成し、終了時に
            manifest も自分で書く。`onboard` verb のように呼び出し側で `RunDir`
            を先に作ってから渡すパターンに対応するための引数。

    Returns:
        使用した（あるいは新規作成した）`RunDir`。
    """
    # 遅延 import: Mitsuba 初期化を mrender ヘルプ表示等で巻き込まないため。
    from satellite_orbit import print_run_summary, render_frame

    config, objects, earth_day_texture = resolve_run_inputs(args)
    start_frame, end_frame = frame_range(args)
    # 解決済み区間を args にも書き戻しておく（manifest にこの値が残る）。
    args.start_frame = start_frame
    args.end_frame = end_frame

    primary_name = getattr(args, 'primary_name', None) or objects[0].name
    # `owns_run=True` なら manifest 書き込みは自分で。False なら呼び出し側に任せる。
    owns_run = run_dir is None
    if run_dir is None:
        run_dir = create_run_dir('render', args, name_hint=primary_name)
    # satellite_orbit 側は args.output_dir を見るので、ランディレクトリで上書き。
    args.output_dir = str(run_dir.path)
    frames_dir = run_dir.frames_dir

    print_run_summary(args, objects, config, run_dir.path, earth_day_texture, start_frame, end_frame)

    status = 'ok'
    extra: Dict[str, object] = {}
    try:
        if not getattr(args, 'no_frames', False):
            # 物体ごとの軌跡（trail）描画用に履歴を持ち回す。
            trail_histories: Dict[str, List[np.ndarray]] = {}
            for frame in range(start_frame, end_frame):
                render_frame(frame, args.frames, objects, config, frames_dir, trail_histories)
        else:
            print('注意: --no-frames が指定されたためフレーム出力をスキップしました。')
        extra['frame_count'] = end_frame - start_frame
        extra['frames_dir'] = str(frames_dir)
    except Exception as exc:
        # 例外でも manifest にステータスを残せるよう、status を上書きしてから再送出。
        status = f'error: {exc!r}'
        raise
    finally:
        if owns_run:
            run_dir.write_manifest(args, finished_at=datetime.now(), status=status, extra=extra)
        else:
            # 親 verb が manifest を書く場合は、render 固有の追加情報だけ預ける。
            run_dir.manifest_extra.setdefault('render', {}).update(extra)

    print(f"\n{'=' * 60}")
    print(f'完了: {end_frame - start_frame} フレームを処理しました。')
    print(f'出力: {frames_dir}/frame_*.png')
    print(f'ランディレクトリ: {run_dir.path}')
    if getattr(args, 'make_video', False):
        # YAML / CLI で動画化指示があれば ffmpeg を呼ぶ。失敗しても致命にはしない。
        video_path = maybe_make_video(
            frames_dir,
            fps=float(getattr(args, 'video_fps', 30.0) or 30.0),
            output_name=str(getattr(args, 'video_name', 'output.mp4') or 'output.mp4'),
            enabled=True,
        )
        if video_path is not None and not owns_run:
            run_dir.manifest_extra.setdefault('render', {})['video'] = str(video_path)
        elif video_path is not None:
            extra['video'] = str(video_path)
    print(f"{'=' * 60}")
    return run_dir


def main(argv=None) -> None:
    """CLI エントリポイント。`mrender render ...` および直接実行から呼ばれる。"""
    args = resolve_args(argv)
    run(args)


if __name__ == '__main__':
    main()
