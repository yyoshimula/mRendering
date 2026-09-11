# Third-party notices / 同梱データ・ライブラリの出典

このリポジトリのソースコードは [BSD 3-Clause License](LICENSE) で配布しますが、
同梱しているデータやライブラリには**それぞれ別のライセンス**が適用されます。
再配布・派生物の公開時は下記の条件（帰属表示など）を守ってください。

## 星表・天体データ

| ファイル | 出典 | ライセンス / 条件 |
|---|---|---|
| `tools/hyg_catalog.csv` | **HYG Database v4.1** (David Nash, [astronexus.com/hyg](https://www.astronexus.com/hyg)) — Hipparcos / Yale Bright Star / Gliese を統合した星表 | **CC BY-SA 4.0**。この CSV と、そこから生成される `assets/starfield.exr`（`tools/generate_starfield.py`）は BSD ではなく CC BY-SA 4.0 の条件で扱うこと（帰属表示、派生データは同一ライセンス） |
| `assets/hipparcos.npz` | **Hipparcos Catalogue** (ESA, 1997) の抜粋 | ESA の学術利用条件。出典表示: "ESA, 1997, The Hipparcos and Tycho Catalogues, ESA SP-1200" |
| （実行時取得）Gaia DR3 | ESA Gaia、VizieR TAP 経由 | 帰属表示: ESA/Gaia/DPAC |

## 3D モデル

| ファイル | 出典 | 条件 |
|---|---|---|
| `models/hubble_a.glb`, `models/hubble.obj`, `models/hubble_textures/` | NASA 3D Resources "Hubble Space Telescope (A)" を OBJ + PNG に変換 | NASA の素材は原則として著作権フリー（米国政府著作物）。ただし **NASA による推奨・関与を示唆する使い方は不可**（[NASA Media Usage Guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/)）。NASA のロゴ・記章は商標として別扱い |
| `models/iss.glb`, `models/iss.obj` | NASA 3D Resources "International Space Station" | 同上 |
| `models/aqua.glb`, `models/aqua.obj` | NASA 3D Resources "Aqua" | 同上 |
| `models/test_cube.obj` | 本リポジトリで作成 | BSD 3-Clause |
| `models/library/*/materials.json` | 本リポジトリで作成した材質割り当てメタデータ（形状データ本体は含まない） | BSD 3-Clause |

## 地球テクスチャ

| ファイル | 出典 | 条件 |
|---|---|---|
| `assets/textures/earth_day.jpg`, `earth_day_8k.png`, （任意取得）`earth_day_500m/` | NASA Visible Earth **Blue Marble Next Generation** (2004) | 帰属表示: "NASA Earth Observatory / Blue Marble Next Generation" |
| `assets/textures/earth_night.jpg`, `models/night_textures/` | NASA Earth Observatory **Black Marble / Earth at Night**（夜光テクスチャは本リポジトリで加工） | 帰属表示: "NASA Earth Observatory" |
| `assets/textures/earth_clouds.jpg`, `models/cloud_textures/cloud_opacity.png` | NASA Visible Earth Blue Marble 雲レイヤ | 帰属表示: "NASA Visible Earth" |
| （実行時取得）GIBS 日次実写画像 | NASA GIBS (Global Imagery Browse Services) | 帰属表示: "NASA GIBS / Worldview" |
| （実行時取得）MODIS 雲分率・雲光学的厚さ・地表アルベドの科学レイヤとカラーマップ（`earth_albedo_map.py`、`runs/_gibs_cache/`） | NASA GIBS 経由の MODIS (MOD06 / MCD43) | 帰属表示: "NASA GIBS / Worldview (MODIS)" |

## 同梱ライブラリ

| パス | ライブラリ | ライセンス |
|---|---|---|
| `gui/vendor/three/` | [three.js](https://threejs.org/) | MIT（`gui/vendor/three/LICENSE`） |
| `gui/vendor/three/examples/jsm/libs/draco/` | Google **Draco** デコーダ | Apache License 2.0 |
| `yoshimulib/` | [yoshimulib](https://github.com/yyoshimula/yoshimulib) の部分集合（`yoshimulib/VENDORED.md`） | MIT（`yoshimulib/LICENSE`） |

## 派生コード

| パス | 出典 | ライセンス / 条件 |
|---|---|---|
| `tools/pv_earthshine_reference.py` | Fankhauser, Tyson & Askari (2023) "Satellite Optical Brightness", AJ 166, 59 の公開コード **Lumos**（[lumos-sat](https://pypi.org/project/lumos-sat/) 1.0.9, [satellite-optical-brightness](https://github.com/Forrest-Fankhauser/satellite-optical-brightness)）の地球パネル格子（`get_earthshine_panels`）と BRDF ライブラリを放射照度計算用に書き直したもの。収録の Phong フィット値も同リポジトリ由来 | **MIT License**, Copyright (c) 2023 Forrest Fankhauser（帰属表示を保持すること） |

## 実行時依存（pip、同梱しない）

Mitsuba 3（BSD 3-Clause）、Dr.Jit（BSD 3-Clause）、NumPy / SciPy（BSD）、PyYAML（MIT）、
sgp4（MIT）、trimesh（MIT）、Pillow（MIT-CMU）。
