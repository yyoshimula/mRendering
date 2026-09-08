#!/usr/bin/env python3
"""NASA Blue Marble NG 500 m/px 地球テクスチャをサブタイル化して配置する。

relative verb の地球可視域クロップ（relative_motion._crop_equirect_texture）が
使う「タイル分割 equirect ソース」を生成する一回きりの準備スクリプト。

ソース: NASA Visible Earth "Blue Marble Next Generation w/ Topography and
Bathymetry" (August 2004, image record 73776)。全球 86400×43200 px
（赤道で 463 m/px）が 21600×21600 の大タイル 8 枚（A1..D2、計 ~420 MB）で
配布されている。1 枚まるごとの JPEG デコードは ~1.4 GB になるため、
レンダラが可視域だけを軽く読めるよう 2700×2700 の小タイル 512 枚
（cols=32 × rows=16）に切り直し、meta.json を添えて出力する。

使い方:
  python tools/prepare_bmng.py                 # DL から実行（~420 MB）
  python tools/prepare_bmng.py --src-dir DIR   # DL 済み bmng_A1.jpg 等を使う
  python tools/prepare_bmng.py --out assets/textures/earth_day_500m

出力レイアウト（幅 86400 の全球 equirect を格子に割ったもの）:
  out/meta.json                    … {"width","height","tile","cols","rows",
                                      "pattern","quality","source"}
  out/tile_r00_c00.jpg ...         … 行 r（北から）・列 c（西経 180° から東へ）
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

BASE_URL = ('https://eoimages.gsfc.nasa.gov/images/imagerecords/73000/73776/'
            'world.topo.bathy.200408.3x21600x21600.{tile}.jpg')
# 大タイルの並び: 列 A..D = 経度 -180°→+180°、行 1..2 = 北半球/南半球
BIG_COLS = ['A', 'B', 'C', 'D']
BIG_ROWS = ['1', '2']
BIG_PX = 21600
SUB_PX = 2700          # 2700×2700 → 大タイル 1 枚 = 8×8 サブタイル
JPEG_QUALITY = 92


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--src-dir', type=str, default=None,
                    help='DL 済み bmng_{A1..D2}.jpg のあるディレクトリ（未指定なら DL）')
    ap.add_argument('--out', type=str, default='assets/textures/earth_day_500m')
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n_sub = BIG_PX // SUB_PX  # 大タイル内のサブタイル数（片辺）
    cols, rows = n_sub * len(BIG_COLS), n_sub * len(BIG_ROWS)

    for bi, brow in enumerate(BIG_ROWS):
        for bj, bcol in enumerate(BIG_COLS):
            name = f'{bcol}{brow}'
            if args.src_dir:
                src = Path(args.src_dir) / f'bmng_{name}.jpg'
            else:
                src = out / f'_dl_{name}.jpg'
                if not src.exists():
                    url = BASE_URL.format(tile=name)
                    print(f'download {url}')
                    urllib.request.urlretrieve(url, src)
            print(f'subtile {name} <- {src}')
            arr = np.asarray(Image.open(src).convert('RGB'))
            if arr.shape[:2] != (BIG_PX, BIG_PX):
                sys.exit(f'unexpected size {arr.shape} for {src}')
            for si in range(n_sub):
                for sj in range(n_sub):
                    r, c = bi * n_sub + si, bj * n_sub + sj
                    sub = arr[si * SUB_PX:(si + 1) * SUB_PX,
                              sj * SUB_PX:(sj + 1) * SUB_PX]
                    Image.fromarray(sub).save(out / f'tile_r{r:02d}_c{c:02d}.jpg',
                                              quality=JPEG_QUALITY)
            del arr
            if not args.src_dir:
                src.unlink()  # DL した大タイルは分割後に削除（再取得可能）

    meta = {'width': cols * SUB_PX, 'height': rows * SUB_PX,
            'tile': SUB_PX, 'cols': cols, 'rows': rows,
            'pattern': 'tile_r{row:02d}_c{col:02d}.jpg',
            'quality': JPEG_QUALITY,
            'source': 'NASA Visible Earth, Blue Marble Next Generation '
                      'w/ Topography and Bathymetry (Aug 2004, record 73776)'}
    (out / 'meta.json').write_text(json.dumps(meta, indent=2))
    print(f'done: {out}/  ({cols}x{rows} tiles of {SUB_PX}px, '
          f'{cols * SUB_PX}x{rows * SUB_PX} px global)')


if __name__ == '__main__':
    main()
