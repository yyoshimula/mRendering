"""`mrender onboard` 動詞: 機載カメラ視点で別物体を注視するレンダリング。

`render` verb と同じ実装を使うが、`view_mode=satellite` を既定にして
`view_camera_name` / `view_target_name` をプリセットで指定する想定の薄い
エイリアス。Case B（軌道上の 2 機 — 片方視点で片方を見る）の入口として用意。

実装的には RunDir だけ自分で作って `verbs.render.run()` に委譲しているので、
出力レイアウト・ffmpeg 動画化・manifest の扱いは render と同等になる。
"""

from __future__ import annotations

import argparse

from runs import RunDir, create_run_dir
from verbs.render import run as run_render
from verbs._common import resolve_args


def run(args: argparse.Namespace, run_dir: RunDir | None = None) -> RunDir:
    """機載カメラ視点で 1 ラン分のレンダを行う。

    `view_mode` が未設定または 'inertial' のときだけ 'satellite' に上書きする
    （プリセット側で 'satellite' 以外を明示している場合は尊重する）。
    RunDir を `'onboard'` verb として作っておけば、内部で render に委譲しても
    manifest 上は onboard ランとして残る。
    """
    if not getattr(args, 'view_mode', None) or args.view_mode == 'inertial':
        args.view_mode = 'satellite'
    # ディレクトリ名はカメラ機体名から付ける（例 `..._onboard_chief`）。
    name_hint = getattr(args, 'view_camera_name', None) or 'onboard'
    if run_dir is None:
        run_dir = create_run_dir('onboard', args, name_hint=name_hint)
    # render の `run()` に丸投げ。manifest も向こうが書く（owns_run=False ではない）。
    return run_render(args, run_dir=run_dir)


def main(argv=None) -> None:
    """CLI エントリポイント。`mrender onboard ...` から呼ばれる。"""
    args = resolve_args(argv)
    run(args)


if __name__ == '__main__':
    main()
