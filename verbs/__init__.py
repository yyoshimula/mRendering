"""mrender サブコマンド本体を集めたパッケージ。

各モジュール（`render`, `lightcurve`, `preview`, `rotation`, `relative`, `onboard`）
が 1 つの verb を実装する。それぞれ `main(argv)` と `run(args, run_dir=None)` を
公開し、`mrender.py` の `_dispatch()` から呼び出される。

共通の引数解決（YAML マージ、RenderConfig 構築、ffmpeg 動画化）は
`verbs._common` に切り出してある。
"""
