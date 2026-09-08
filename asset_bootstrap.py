"""初回起動時に足りない（または規約が古い）生成アセットを作る（現状 starfield.exr のみ）。

`assets/starfield.exr`（8192x4096, 約 287 MB）は HYG カタログから決定論的に
生成できるため、GitHub の 100 MB 上限を避けて Git 管理外にしてある。
配布版を clone した直後は存在しないので、Mitsuba へ渡す直前にここで生成する。

生成は `tools/generate_starfield.py --width 8192 --milky-way` と同一で、
リポジトリで使ってきた EXR とビット単位で一致する（実測 ~0.8 秒）。
"""

import json
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

# 投影規約のバージョン。generate_starfield.py が EXR と並べて `<exr>.json` に書き、
# ここで照合する。不一致（2026-09-08 以前の u=RA/2π 規約 = 鏡像）や欠落なら作り直す。
#   skychart-v2: u = 1 − RA/2π, v = 1/2 − Dec/π（内側から見た星図）+ 線形黒体色。
#                optical_lighting.STARFIELD_LOCAL_TO_ECI を to_world に合成して使う。
STARFIELD_CONVENTION = 'skychart-v2'


def starfield_sidecar_path(exr_path: Path) -> Path:
    return exr_path.with_name(exr_path.name + '.json')


def write_starfield_sidecar(exr_path: Path, **extra) -> None:
    meta = {'projection': STARFIELD_CONVENTION, **extra}
    starfield_sidecar_path(exr_path).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n')


def starfield_needs_regeneration(exr_path: Path) -> str:
    """作り直す理由（空文字なら不要）。EXR 欠落 / サイドカー欠落 / 規約不一致。"""
    if not exr_path.exists():
        return 'missing'
    sidecar = starfield_sidecar_path(exr_path)
    if not sidecar.exists():
        return 'no-sidecar (2026-09-08 以前の旧規約 EXR の可能性)'
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return 'unreadable-sidecar'
    if meta.get('projection') != STARFIELD_CONVENTION:
        return f"projection {meta.get('projection')!r} != {STARFIELD_CONVENTION!r}"
    return ''


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
    if resolved != target:
        return p
    reason = starfield_needs_regeneration(resolved)
    if not reason:
        return p

    print(f'assets/starfield.exr を HYG カタログから生成します（理由: {reason}、約 3 秒）',
          flush=True)
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
        tmp_sidecar = starfield_sidecar_path(tmp)
        if tmp_sidecar.exists():
            os.replace(tmp_sidecar, starfield_sidecar_path(resolved))
        else:
            write_starfield_sidecar(resolved)
    finally:
        for leftover in (tmp, starfield_sidecar_path(tmp)):
            if leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass
    return p
