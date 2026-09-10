#!/usr/bin/env python3
"""mRender.app のアイコン元画像 build/app_icon_1024.png を生成する。

あかつき（internal/models/akatsuki.obj、金色 Au 導体 + 青パドル）を黒背景・
正面寄りの太陽光でレンダし、角丸マスクだけ掛けた 1024px PNG を書き出す
（build_app.sh が .icns 化する）。レンダには Mitsuba が要るので venv で実行。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / 'build' / 'app_icon_1024.png'

# アイコン専用の relative 設定（材質は internal/presets/akatsuki.yaml と同一。研究室内限定モデル）
CONFIG = """\
rendering: {frames: 1, samples: 256, width: 1024, height: 1024, make_video: false}
mode: static
deputy:
  rel_position: [0.0095, 0.0, 0.0]
  rel_quat: [0.2346, -0.1094, 0.4082, 0.8754]  # パドルを対角に、ホーンアンテナが上
  deputy_model: internal/models/akatsuki.obj
  deputy_scale: 0.0001
  deputy_bsdf:
    type: roughplastic
    diffuse_reflectance: {type: rgb, value: [0.8, 0.8, 0.7]}
    alpha: 0.3
    int_ior: 1.5
  deputy_parts:
    PLANET-C_v24:
      type: roughconductor
      distribution: ggx
      alpha: 0.2
      material: Au
    sap1:
      type: diffuse
      reflectance: {type: rgb, value: [0.05, 0.05, 0.3]}
    sap2:
      type: diffuse
      reflectance: {type: rgb, value: [0.05, 0.05, 0.3]}
camera:
  camera_origin: [0.0, 0.0, 0.0]
  camera_target: [0.0095, 0.0, 0.0]
  camera_fov: 34.0
  camera_up: [0.0, 0.0, 1.0]
environment:
  hide_chief: true
  show_earth: false
  env_brightness: 0.0                # 定数環境光は背景に写るので 0 = 純黒背景
  sun_direction: [0.85, -0.35, -0.4] # 正面寄り右上 → 金箔が輝く
  sun_irradiance: 6.0
  sun_temperature: 5778.0
  max_depth: 8
visualization: {show_inertial_axes: false, show_body_axes: false}
"""


def render_satellite(tmp: Path) -> Image.Image:
    cfg = tmp / 'icon_config.yaml'
    cfg.write_text(CONFIG)
    subprocess.run(
        [sys.executable, 'mrender.py', 'relative',
         '--config', str(cfg),
         '--output-dir', str(tmp / 'run')],
        cwd=REPO, check=True, capture_output=True)
    return Image.open(tmp / 'run' / 'frames' / 'frame_0000.png').convert('RGB')


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        sat = render_satellite(Path(td))

    size = sat.size[0]
    # 黒背景はレンダそのまま。macOS 風の角丸マスクだけ掛ける
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [size * 0.06, size * 0.06, size * 0.94, size * 0.94],
        radius=int(size * 0.2), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2))
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(sat, (0, 0), mask)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.save(OUT)
    print(f'wrote {OUT}')


if __name__ == '__main__':
    main()
