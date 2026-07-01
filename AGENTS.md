# AGENTS.md

このファイルは、Codex (Codex.ai/code) がこのリポジトリで作業する際のガイダンスを提供します。

## プロジェクト概要

Mitsuba 3を使用した物理ベースの人工衛星軌道レンダリングシステム。ケプラー軌道力学、姿勢制御、高度な光学シミュレーションにより、地球を周回する衛星のフォトリアリスティックなアニメーションをレンダリングします。

## コマンド

### セットアップ
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# yoshimulibの依存（numpy, scipy, matplotlib, sympy）も必要
pip install -r yoshimulib/requirements.txt
```

### 実行（satellite_orbit.py: 軌道レンダリング）
```bash
# YAMLプリセットで実行
python satellite_orbit.py --config presets/iss_basic.yaml

# プリセット + CLIオーバーライド（CLI引数 > YAML > デフォルト）
python satellite_orbit.py --config presets/iss_basic.yaml --frames 10 --samples 4

# 基本的なISS軌道（CLI直接指定）
python satellite_orbit.py --altitude 408 --inclination 51.6 --frames 30

# 外部3Dモデル使用（OBJ/PLY/GLB）
python satellite_orbit.py --altitude 408 \
    --satellite-model models/iss.obj --satellite-scale 0.003 --frames 30

# デブリに外部モデル＋材質オーバーライド
python satellite_orbit.py --altitude 408 --football --football-distance 1.0 \
    --debris-model models/target.ply --debris-scale 0.001 \
    --debris-model-material gold --view-mode debris --auto-fov --frames 60
```

### 実行（simple_rotation.py: 単機タンブリング、軌道なし）
```bash
# mrender 経由（推奨、runs/ に自動配置）
python mrender.py rotation --config presets/akatsuki.yaml

# スタンドアロン
python simple_rotation.py --config presets/akatsuki.yaml
python simple_rotation.py --config presets/akatsuki.yaml --frames 10 --samples 4
python simple_rotation.py --frames 30
python simple_rotation.py --model-path models/akatsuki.obj --model-scale 0.1 --frames 10
```

### 実行（relative_motion.py: 2 機の相対配置、軌道なし）
```bash
# mrender 経由（推奨、runs/ に自動配置）
python mrender.py relative --config presets/relative_static.yaml

# スタンドアロン: static / csv / tumble モード
python relative_motion.py --frames 30 --rel-position 1.5 0 0
python relative_motion.py --mode csv --rel-csv input/rel_state_sample.csv --frames 60
python relative_motion.py --mode tumble --rel-position 1.5 0 0 --wx 0.3 --wz 1.0
```

### 動画作成（ffmpeg必要）
```bash
ffmpeg -framerate 30 -i output/frame_%04d.png -c:v libx264 -pix_fmt yuv420p output.mp4
```

## アーキテクチャ

### モジュール構成
- **mrender.py** - 統合 CLI（render / lightcurve / preview / rotation / relative）
- **satellite_orbit.py** - render/lightcurve/preview のコア：軌道計算、シーン生成、レンダリングループ
- **simple_rotation.py** - rotation のコア：オイラー回転運動方程式、クォータニオン姿勢、OBJパーツ別BSDF（軌道なし）
- **relative_motion.py** - relative のコア：相対位置・相対姿勢で 2 機を配置（軌道なし、static/csv/tumble モード）
- **verbs/** - mrender サブコマンドのシム
- **optical_materials.py** - `MaterialLibrary`クラス：宇宙機材料のBSDF定義（導体、誘電体、複合材）
- **optical_lighting.py** - 太陽光/月光の生成、黒体放射色温度、星空環境光
- **optical_atmosphere.py** - レイリー/ミー散乱、大気密度モデル、地球アルベド
- **yoshimulib/** - 自作ライブラリ：軌道力学、姿勢、座標変換等（積極的に使用すること）
- **presets/** - YAMLプリセットファイル（モデル、力学、カメラ、レンダリング設定）

### yoshimulibの使用箇所

satellite_orbit.pyでは以下のyoshimulib関数を使用している。新機能追加時もyoshimulibの関数を優先して使うこと。

| 用途 | yoshimulib関数 | import元 |
|------|---------------|----------|
| 軌道要素→位置/速度 | `oe2rv(oe, flag, mu)` | `yoshimulib.orbit.orbital_elements` |
| 位置/速度→軌道要素 | `rv2oe(r, v, mu)` | `yoshimulib.orbit.orbital_elements` |
| ECI→RTN座標変換 | `dcm_i2rtn(raan, inc, w, nu)` | `yoshimulib.orbit.transforms` |
| 物理定数 | `MU_EARTH_KM`, `R_EARTH_KM`, `AU_KM` | `yoshimulib.orbit.orbital_elements` |

**注意**: yoshimulibはトップレベル`__init__.py`で全サブモジュールをeager importするため、`from yoshimulib.orbit.orbital_elements import ...` のようにサブモジュールを直接指定してimportすること。`from yoshimulib.orbit import ...` だとmatplotlib等の不要な依存が引っ張られる。

### 主要コンポーネント

**軌道力学（yoshimulib使用）：**
- `OrbitalElements` データクラス：6パラメータのケプラー軌道要素（a, e, i, Ω, ω, M₀）
- `compute_orbital_position()`：`oe2rv()` で軌道要素→ECI座標変換
- `compute_lvlh_frame()`：`rv2oe()` + `dcm_i2rtn()` でRTN基底ベクトルを取得

**姿勢制御（yoshimulib使用）：**
- `compute_attitude_matrix()`：NADIRモードは `rv2oe()` + `dcm_i2rtn()` でRTN基底から姿勢行列を構成
- モード：NADIR（地球指向）、SUN_TRACKING（太陽追尾）、VELOCITY_ALIGNED（速度整列）

**シーン生成：**
- `create_satellite()`：手続き的3Dモデル生成（本体、ソーラーパネル、アンテナ）
- `load_external_model()`：外部OBJ/PLYファイル読み込み（`create_satellite`/`create_debris`の代替）
- `create_earth()`：テクスチャマッピング対応の球体
- `create_scene()`：Mitsubaシーン辞書の組み立て

**外部3Dモデル読み込み：**
- `resolve_material_by_name()`：材質名→BSDF辞書変換（aluminum_brushed, gold, solar_panel等9種）
- `load_external_model()`：OBJ/PLY読み込み、Transform・材質適用、フォールバック対応
- 未指定時は従来のプロシージャルモデルにフォールバック

### YAMLコンフィグ共通パターン（satellite_orbit.py / simple_rotation.py 共通）
```python
# load_yaml_config(): ネストYAMLを1段フラット化 → argparse dest名の辞書
# parser.parse_known_args() → set_defaults() → parse_args()
# 優先順位: CLI引数 > YAML > argparseデフォルト
```
- YAMLのセクション名（`model:`, `dynamics:`等）は組織用で、中のキーがフラット化される
- YAMLキー名はargparse dest名と一致させる（例: `model_path`, `camera_origin`）
- dict型の値（`model_bsdf`, `model_parts`等）もset_defaultsでargsに載る
- `--config nargs='+'` で複数YAML指定可能（後のファイルが優先）

### データフロー
```
CLIオプション → OrbitalElements → メインループ:
  compute_orbital_position() [oe2rv] → 位置/速度
  compute_attitude_matrix()  [dcm_i2rtn] → 回転行列
  load_external_model() or create_satellite() → 衛星メッシュ
  create_scene() → Mitsubaシーン辞書
  mi.render() → HDR画像 → PNG出力
```

### レンダリング設定
- バリアント：`scalar_rgb`（CPUベースパストレーシング）
- デフォルト：128サンプル/ピクセル、12バウンス深度
- 出力：`output/`ディレクトリにPNGフレーム

## 主要な定数（yoshimulibから取得）
- 地球赤道半径：6378.137 km（WGS84）
- 重力パラメータ：398,600.4418 km³/s²
- 天文単位：149,597,870.7 km
- デフォルト高度：408 km（ISS）
- 太陽色温度：5778 K
