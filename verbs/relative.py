"""`mrender relative` 動詞: 相対位置・相対姿勢のみを与えて 2 機をレンダする。

軌道力学を使わず、chief / deputy の相対状態を直接与えるシーン
（`relative_motion.py` の処理本体）を verb として呼び出す。
モードは static（固定）/ csv（時系列ファイル）/ tumble（タンブリング） の 3 種類で、
`relative_motion.parse_args` がそれを解釈する。
"""

from __future__ import annotations

import argparse
from datetime import datetime

from runs import RunDir, create_run_dir
from verbs._common import maybe_make_video


def run(args: argparse.Namespace, run_dir: RunDir | None = None) -> RunDir:
    """1 ラン分のレンダを `runs/<ts>_relative_<name>/frames/` に書き出す。

    `relative_motion._run` は `args.output_dir` を frames 保存先として使うため、
    そこに RunDir の `frames_dir` を渡している点に注意。
    """
    # Mitsuba 初期化を遅延させるため関数内で import。
    from relative_motion import _run as _run_relative

    name_hint = getattr(args, 'name', None) or getattr(args, 'primary_name', None) or 'relative'
    owns_run = run_dir is None
    if run_dir is None:
        run_dir = create_run_dir('relative', args, name_hint=name_hint)
    args.output_dir = str(run_dir.frames_dir)

    status = 'ok'
    extra = {}
    try:
        _run_relative(args)
        extra['frame_count'] = int(getattr(args, 'frames', 0))
        extra['frames_dir'] = str(run_dir.frames_dir)

        # 動画化は manifest 書き込み（finally）より前に済ませる。
        # 後回しにすると 'video' キーが manifest に載らない。
        if getattr(args, 'make_video', False):
            # `--video-fps` 優先、それが無ければ `--fps`、最後に 30 fps をデフォルト。
            video_path = maybe_make_video(
                run_dir.frames_dir,
                fps=float(getattr(args, 'video_fps', None) or getattr(args, 'fps', 30.0) or 30.0),
                output_name=str(getattr(args, 'video_name', 'output.mp4') or 'output.mp4'),
                enabled=True,
            )
            if video_path is not None:
                extra['video'] = str(video_path)
    except Exception as exc:  # noqa: BLE001
        status = f'error: {exc!r}'
        raise
    finally:
        if owns_run:
            run_dir.write_manifest(args, finished_at=datetime.now(), status=status, extra=extra)
        else:
            run_dir.manifest_extra.setdefault('relative', {}).update(extra)

    print(f'\nランディレクトリ: {run_dir.path}')
    return run_dir


def main(argv=None) -> None:
    """CLI エントリポイント。引数パーサは `relative_motion.parse_args` を使用。"""
    from relative_motion import parse_args
    args = parse_args(argv)
    run(args)


if __name__ == '__main__':
    main()
