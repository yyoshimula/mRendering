#!/usr/bin/env python3
"""Mitsuba バリアント選択の唯一の実装。

環境変数 ``MRENDER_VARIANT`` にカンマ区切りで希望バリアントを並べると、
利用可能な最初のものが選ばれる（Mitsuba の ``set_variant`` の複数指定を利用）。
未設定なら従来どおり ``llvm_ad_rgb``。

    MRENDER_VARIANT=cuda_ad_rgb python mrender.py render --config ...
    MRENDER_VARIANT=cuda_ad_rgb,llvm_ad_rgb python mrender.py gui   # CUDA 優先、無ければ LLVM

DGX Spark（Grace ARM64 + Blackwell GPU）ではソースビルドで cuda_ad_rgb を
有効化し、この環境変数だけで GPU レンダリングに切り替える想定
（tools/dgx/README.md 参照）。

バッチとライブプレビューのピクセル一致検証はバリアント単位で成り立つ。
同一実行系（gui_server → live_worker と各 verb サブプロセス）は環境変数を
継承するので常に同じバリアントになるが、CUDA と LLVM の間でビット一致は
保証されない点に注意。
"""

import os

DEFAULT_VARIANT = 'llvm_ad_rgb'


def init_variant(mi):
    """Mitsuba バリアントを設定して名前を返す。既に設定済みなら no-op。"""
    if mi.variant():
        return mi.variant()
    wanted = [v.strip() for v in os.environ.get('MRENDER_VARIANT', '').split(',')
              if v.strip()]
    if DEFAULT_VARIANT not in wanted:
        wanted.append(DEFAULT_VARIANT)
    mi.set_variant(*wanted)
    return mi.variant()
