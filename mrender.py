#!/usr/bin/env python3
"""mrender: 軌道シミュレーション × Mitsuba レンダラの統合 CLI。

このファイルはユーザがコマンドラインから叩くトップレベルのエントリポイントで、
最初の引数（verb）を見て `verbs/` 以下のサブコマンド実装にディスパッチするだけの
薄いルータである。実体の処理は各 verb モジュール（`verbs.render` など）にある。

サブコマンド:
  render      フレーム連番を出力（従来の satellite_orbit.py 相当）
  lightcurve  主物体の地上観測ライトカーブを CSV 出力
  preview     1 フレームだけ即レンダ（材質/ライティング調整用）
  rotation    単機のオイラー回転運動（タンブリング）をレンダ
  relative    軌道力学なし、相対位置・相対姿勢のみで 2 機をレンダ
  onboard     render の機載カメラ専用エイリアス（view_mode=satellite 既定）
  groundobs   地上望遠鏡からの光学観測（見かけ等級ライトカーブ + 望遠鏡像）

すべての動詞は `--config` で YAML プリセットを読み、`runs/<ts>_<verb>_<name>/`
配下に成果物と manifest.json を出力する。
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

# ディスパッチ可能な verb（サブコマンド）一覧。ここに無い名前は弾かれる。
VERBS = ('render', 'lightcurve', 'preview', 'rotation', 'relative', 'onboard',
         'groundobs', 'gui')


def _print_usage(stream=sys.stderr) -> None:
    """簡易 usage を表示する。`--help` / 未知 verb / 引数なし時に呼ばれる。"""
    print(
        "usage: mrender <verb> [verb args]\n"
        "  verbs: " + ', '.join(VERBS) + "\n"
        "  例: python mrender.py render --config presets/iss.yaml\n"
        "      python mrender.py lightcurve --config presets/iss.yaml\n"
        "      python mrender.py preview --config presets/iss.yaml presets/earth_beauty.yaml"
        "  # 後勝ちで差分 YAML を重ねる\n"
        "      python mrender.py rotation --config presets/rotation_hubble.yaml\n"
        "      python mrender.py relative --config presets/relative_static.yaml\n"
        "      python mrender.py groundobs --config presets/groundobs_hubble.yaml",
        file=stream,
    )


def _dispatch(verb: str, rest: List[str]) -> None:
    """verb 名から対応するモジュールの `main(argv)` を呼び出す。

    Mitsuba の重い初期化を避けるため、verb モジュールは「実際に必要になった時点」で
    初めて import する（遅延 import）。これにより `--help` 表示などが軽量になる。

    Args:
        verb: サブコマンド名（VERBS のいずれか）。
        rest: verb 以降に並ぶ CLI 引数（そのまま verb の `main` に渡す）。
    """
    if verb == 'render':
        from verbs.render import main as run
    elif verb == 'lightcurve':
        from verbs.lightcurve import main as run
    elif verb == 'preview':
        from verbs.preview import main as run
    elif verb == 'rotation':
        from verbs.rotation import main as run
    elif verb == 'relative':
        from verbs.relative import main as run
    elif verb == 'onboard':
        from verbs.onboard import main as run
    elif verb == 'groundobs':
        from verbs.groundobs import main as run
    elif verb == 'gui':
        from verbs.gui import main as run
    else:
        _print_usage()
        raise SystemExit(2)
    run(rest)


def main(argv: Optional[List[str]] = None) -> None:
    """CLI エントリポイント。`argv` 省略時は `sys.argv[1:]` を使う。

    フロー: 引数なし or help → usage を出して終了 / 第 1 引数を verb として
    `_dispatch` に投げる。verb が未知なら exit code 2 で終了。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        _print_usage(sys.stdout)
        return
    verb, *rest = argv
    if verb not in VERBS:
        print(f"mrender: 未対応のサブコマンド: {verb}", file=sys.stderr)
        _print_usage()
        raise SystemExit(2)
    _dispatch(verb, rest)


if __name__ == '__main__':
    main()
