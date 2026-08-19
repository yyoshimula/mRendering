"""`mrender gui` 動詞: ブラウザ GUI サーバーを起動する。

実体は `gui_server.py`。Mitsuba は import しないため起動は軽量。
レンダリング自体は GUI からサブプロセス（`mrender.py <verb> --config ...`）
として起動されるので、この verb 自身は HTTP サーバーを回すだけ。

使い方:
    python mrender.py gui                # http://127.0.0.1:8600 をブラウザで開く
    python mrender.py gui --port 8765 --no-browser
"""

from __future__ import annotations

from gui_server import main as _main


def main(argv=None) -> None:
    """CLI エントリポイント。`mrender gui ...` から呼ばれる。"""
    _main(argv)


if __name__ == '__main__':
    main()
