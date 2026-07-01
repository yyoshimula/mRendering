"""`mrender rotation` 動詞: 単機のオイラー回転運動（タンブリング）をレンダする。

軌道計算は行わず、慣性主軸まわりのオイラー回転運動方程式とクォータニオン姿勢
だけを積分して機体を回し、Mitsuba で連番を吐く。実体は `simple_rotation.py` の
`_run()`。本ファイルは出力先を RunDir に向け替えて manifest を書くだけの薄い
ラッパ。引数パーサも `simple_rotation.parse_args` を使う（共通パーサとは別系統）。
"""

from __future__ import annotations

import argparse
from datetime import datetime

from runs import RunDir, create_run_dir


def run(args: argparse.Namespace, run_dir: RunDir | None = None) -> RunDir:
    """1 ラン分のタンブリング連番を `runs/<ts>_rotation_<name>/frames/` へ書き出す。

    `simple_rotation._run` は `args.output_dir` をフレーム保存先として読むため、
    ここでは `run_dir.frames_dir` をその値にすり替えてから呼ぶ。
    """
    from simple_rotation import _run as _run_rotation

    # 名前は --name → --primary-name → 'rotation' の順でフォールバック。
    name_hint = getattr(args, 'name', None) or getattr(args, 'primary_name', None) or 'rotation'
    owns_run = run_dir is None
    if run_dir is None:
        run_dir = create_run_dir('rotation', args, name_hint=name_hint)
    # simple_rotation 側はこの値を「フレーム保存先」として使うので frames_dir を渡す。
    args.output_dir = str(run_dir.frames_dir)

    status = 'ok'
    extra = {}
    try:
        _run_rotation(args)
        extra['frame_count'] = int(getattr(args, 'frames', 0))
        extra['frames_dir'] = str(run_dir.frames_dir)
    except Exception as exc:  # noqa: BLE001
        status = f'error: {exc!r}'
        raise
    finally:
        if owns_run:
            run_dir.write_manifest(args, finished_at=datetime.now(), status=status, extra=extra)
        else:
            run_dir.manifest_extra.setdefault('rotation', {}).update(extra)

    print(f'\nランディレクトリ: {run_dir.path}')
    return run_dir


def main(argv=None) -> None:
    """CLI エントリポイント。引数パーサは `simple_rotation.parse_args` を使用。"""
    from simple_rotation import parse_args
    args = parse_args(argv)
    run(args)


if __name__ == '__main__':
    main()
