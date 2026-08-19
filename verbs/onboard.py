"""`mrender onboard` 動詞: 機載カメラ視点で別物体を注視するレンダリング。

`render` verb と同じ実装を使うが、`view_mode=satellite` を既定にして
`view_camera_name` / `view_target_name` をプリセットで指定する想定の薄い
エイリアス。Case B（軌道上の 2 機 — 片方視点で片方を見る）の入口として用意。

実装的には RunDir だけ自分で作って `verbs.render.run()` に委譲しているので、
出力レイアウト・ffmpeg 動画化は render と同等になる。ただし RunDir を渡す形に
なる以上 render 側は `owns_run=False` として manifest を書かないので、
manifest.json は本ファイルが責任を持って書く。
"""

from __future__ import annotations

import argparse
from datetime import datetime

from runs import RunDir, create_run_dir
from verbs.render import run as run_render
from verbs._common import resolve_args


def run(args: argparse.Namespace, run_dir: RunDir | None = None) -> RunDir:
    """機載カメラ視点で 1 ラン分のレンダを行う。

    `view_mode` が未設定または 'inertial' のときだけ 'satellite' に上書きする
    （プリセット側で 'satellite' 以外を明示している場合は尊重する）。
    RunDir を `'onboard'` verb として作っておけば、内部で render に委譲しても
    manifest 上は onboard ランとして残る。

    render に RunDir を渡すと向こうは manifest を書かず `manifest_extra['render']`
    に frame_count / frames_dir / video を積むだけなので、ここで try/finally で
    受けて `write_manifest` する（例外時も status に残す）。
    呼び出し側から `run_dir` を渡された場合は manifest もその呼び出し側の責務。
    """
    if not getattr(args, 'view_mode', None) or args.view_mode == 'inertial':
        args.view_mode = 'satellite'
    # ディレクトリ名はカメラ機体名から付ける（例 `..._onboard_chief`）。
    name_hint = getattr(args, 'view_camera_name', None) or 'onboard'
    owns_run = run_dir is None
    if run_dir is None:
        run_dir = create_run_dir('onboard', args, name_hint=name_hint)
    if not owns_run:
        # 外から RunDir を渡された = manifest はその親が書く。委譲するだけ。
        return run_render(args, run_dir=run_dir)

    status = 'ok'
    try:
        run_render(args, run_dir=run_dir)
    except BaseException as exc:  # noqa: BLE001
        # BaseException なのは、モデルパス不正などで argparse が SystemExit を
        # 投げる経路（config_loader.validate_model_paths → parser.error）でも
        # manifest に error を残すため。捕捉後は必ず再送出する。
        status = f'error: {exc!r}'
        raise
    finally:
        # render が manifest_extra['render'] に積んだ情報も一緒に書き出される。
        run_dir.write_manifest(args, finished_at=datetime.now(), status=status)
    return run_dir


def main(argv=None) -> None:
    """CLI エントリポイント。`mrender onboard ...` から呼ばれる。"""
    args = resolve_args(argv)
    run(args)


if __name__ == '__main__':
    main()
