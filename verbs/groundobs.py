"""`mrender groundobs` 動詞: 地上望遠鏡からの宇宙物体光学観測をシミュレートする。

地上局 (lat/lon/alt) から見た見かけ等級のライトカーブ (observation.csv) と、
望遠鏡センサ面の画像連番（点像〜分解像）を出力する。
処理本体は `ground_observation.py`。
"""

from __future__ import annotations

import argparse
from datetime import datetime

from runs import RunDir, create_run_dir
from verbs._common import maybe_make_video


def run(args: argparse.Namespace, run_dir: RunDir | None = None) -> RunDir:
    """1 ラン分を `runs/<ts>_groundobs_<name>/` に書き出す。

    フレーム PNG は `frames/`、observation.csv はランディレクトリ直下に置く
    （`args.csv_path` で `ground_observation._run` に伝える）。
    """
    # Mitsuba 初期化を遅延させるため関数内で import。
    from ground_observation import _run as _run_groundobs

    name_hint = getattr(args, 'name', None) or 'groundobs'
    owns_run = run_dir is None
    if run_dir is None:
        run_dir = create_run_dir('groundobs', args, name_hint=name_hint)
    args.output_dir = str(run_dir.frames_dir)
    args.csv_path = str(run_dir.path / 'observation.csv')

    status = 'ok'
    extra = {}
    try:
        _run_groundobs(args)
        extra['frame_count'] = int(getattr(args, 'frames', 0))
        extra['frames_dir'] = str(run_dir.frames_dir)
        extra['observation_csv'] = args.csv_path
        pv_csv = run_dir.path / str(getattr(args, 'pv_csv_name', None) or 'pv_irradiance.csv')
        if pv_csv.exists():
            extra['pv_irradiance_csv'] = str(pv_csv)

        # 動画化は manifest 書き込み（finally）より前に済ませる。
        if getattr(args, 'make_video', False):
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
            run_dir.manifest_extra.setdefault('groundobs', {}).update(extra)

    print(f'\nランディレクトリ: {run_dir.path}')
    return run_dir


def main(argv=None) -> None:
    """CLI エントリポイント。引数パーサは `ground_observation.parse_args` を使用。"""
    from ground_observation import parse_args
    args = parse_args(argv)
    run(args)


if __name__ == '__main__':
    main()
