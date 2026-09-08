"""初回起動時に足りない生成アセットを作る（現状 starfield.exr のみ）。

`assets/starfield.exr`（8192x4096, 約 287 MB）は HYG カタログから決定論的に
生成できるため、GitHub の 100 MB 上限を避けて Git 管理外にしてある。
配布版を clone した直後は存在しないので、Mitsuba へ渡す直前にここで生成する。

生成は `tools/generate_starfield.py --width 8192 --milky-way` と同一で、
リポジトリで使ってきた EXR とビット単位で一致する（実測 ~0.8 秒）。
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Union

ROOT = Path(__file__).resolve().parent

# 自動生成の対象。ここに解決されるパスだけを面倒みる（ユーザ指定の別 EXR は触らない）。
STARFIELD_PATH = ROOT / 'assets' / 'starfield.exr'
STARFIELD_GENERATOR = ROOT / 'tools' / 'generate_starfield.py'
STARFIELD_ARGS = ['--width', '8192', '--milky-way']


def ensure_starfield_envmap(path: Union[str, Path]) -> Path:
    """`assets/starfield.exr` が無ければ生成してから返す。

    Args:
        path: 使おうとしている envmap EXR のパス。

    Returns:
        `Path(path)`。`ROOT/assets/starfield.exr` 以外はそのまま返す
        （ユーザが用意した別の envmap を勝手に作り直さないため）。
    """
    p = Path(path)
    try:
        # strict=False なので未生成でも解決できる（相対パスは repo ルート基準）。
        resolved = (p if p.is_absolute() else ROOT / p).resolve()
        target = STARFIELD_PATH.resolve()
    except OSError:
        return p
    if resolved != target or resolved.exists():
        return p

    print('assets/starfield.exr が無いので HYG カタログから生成します'
          '（初回のみ、約 1 秒）', flush=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    # 同時起動（GUI と worker など）で書きかけを読ませないよう、一時ファイルに
    # 出してから os.replace でアトミックに差し替える。Mitsuba の Bitmap.write は
    # 拡張子で書式を決めるので、一時名でも .exr で終わらせること。
    tmp = resolved.parent / f'{resolved.stem}.tmp-{os.getpid()}{resolved.suffix}'
    try:
        subprocess.run(
            [sys.executable, str(STARFIELD_GENERATOR), *STARFIELD_ARGS,
             '--output', str(tmp)],
            check=True,
        )
        os.replace(tmp, resolved)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return p
