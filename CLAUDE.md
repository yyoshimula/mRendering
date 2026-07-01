# CLAUDE.md

このファイルは、Claude Code (claude.ai/code) がこのリポジトリで作業する際のガイダンスを提供します。

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

### 実行（mrender.py: 統合 CLI）

**重要**: `render` / `lightcurve` / `preview` / `onboard` 系のパラメータ（軌道要素、フレーム数、サンプル数、外部モデル指定など）は **YAML プリセット経由** で指定する。`config_loader.build_parser()` は `--config` 以外の個別 CLI フラグを登録しない設計なので、`--altitude` や `--frames` を直接渡すことはできない。CLI で上書きしたい場合は、差分を別 YAML に書いて `--config A.yaml B.yaml`（後勝ち）で重ねる。

```bash
# 軌道レンダリング（render verb）
python mrender.py render --config presets/iss_basic.yaml

# 複数 YAML 合成（後の指定が優先）
python mrender.py render --config presets/iss_basic.yaml presets/earth_beauty.yaml

# CSV ephemeris から軌道・姿勢を再生
python mrender.py render --config presets/csv_orbit.yaml

# 1 フレームだけプレビュー（材質・ライティング調整用）
python mrender.py preview --config presets/iss_basic.yaml

# 観測者から見たライトカーブのみ高速計算
python mrender.py lightcurve --config presets/iss_basic.yaml

# 軌道上 2 機 — deputy 視点で chief を注視（Case B）
python mrender.py onboard --config presets/relative_view.yaml
```

### 実行（rotation: 単機タンブリング、軌道なし）

`rotation` verb (`simple_rotation.py`) は専用の CLI フラグを持つ（`--frames`, `--samples`, `--Ix`, `--wx`, `--model-path`, `--model-scale`, `--camera-origin` など）。YAML と CLI を混在可。

```bash
# mrender 経由（推奨、runs/ に自動配置）
python mrender.py rotation --config presets/akatsuki.yaml

# スタンドアロン
python simple_rotation.py --config presets/akatsuki.yaml
python simple_rotation.py --config presets/akatsuki.yaml --frames 10 --samples 4
python simple_rotation.py --frames 30
python simple_rotation.py --model-path models/akatsuki.obj --model-scale 0.1 --frames 10
```

### 実行（relative: 2 機の相対配置、軌道なし）

`relative` verb (`relative_motion.py`) も専用 CLI フラグを持つ（`--mode`, `--rel-position`, `--rel-quat`, `--rel-csv`, `--frames`, `--wx` など）。

```bash
# mrender 経由（推奨、runs/ に自動配置）
python mrender.py relative --config presets/relative_static.yaml

# スタンドアロン: static / csv / tumble モード
python relative_motion.py --frames 30 --rel-position 1.5 0 0
python relative_motion.py --mode csv --rel-csv input/rel_state_hcw.csv --frames 60
python relative_motion.py --mode tumble --rel-position 1.5 0 0 --wx 0.3 --wz 1.0
```

### 動画作成（ffmpeg必要）
```bash
# 自動: YAML の rendering.make_video: true を立てれば render/relative/onboard が完了後に mp4 を吐く
python mrender.py render --config presets/relative_view.yaml

# 手動: 既に出来ているフレーム連番から（出力先は runs/<ts>_<verb>_<name>/frames/）
ffmpeg -framerate 30 -i runs/<RUN>/frames/frame_%04d.png \
    -c:v libx264 -pix_fmt yuv420p runs/<RUN>/output.mp4
```

### CSV 入力スキーマ（2 種類）

| 用途 | 列 | 座標系 | 読み込み元 | 例 |
|---|---|---|---|---|
| 絶対軌道（osculating Keplerian） | `time_s, a_km, e, inc_rad, raan_rad, ome_rad, f_rad [, q1, q2, q3, q4]` | 軌道要素は ECI / オプションの姿勢は ECI→Body | `CsvEphemeris` ([orbit_mechanics.py](orbit_mechanics.py)) | `input/attitudeOrbit.csv`, `input/abs_chief_hcw.csv`, `input/abs_deputy_hcw.csv` |
| 相対状態（位置 + クォータニオン） | `time_s, x, y, z, qx, qy, qz, qw` | **位置: chief の RTN(HCW) 基底で表現された deputy の相対位置** [km] / 姿勢: chief Body → deputy Body の相対回転 | `load_csv_relative_state` ([relative_motion.py](relative_motion.py)) | `input/rel_state_hcw.csv` |

- 位置単位は km（相対状態 CSV はシーン単位としてそのまま使われる）
- 角度はラジアン
- クォータニオンはスカラーラスト（`qx, qy, qz, qw`）。`q1..q4` 表記でも同一順序、読み込み時に自動正規化
- 範囲外の `t` を要求すると端点クランプ＋ stderr に 1 度警告
- コメント行（先頭 `#`）と数値化失敗するヘッダ行はスキップ

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


## cmux Terminal Interaction

このプロジェクトは cmux Terminal App 内で作業する。Claude Code は cmux CLI を使ってペイン分割・コマンド実行・通知を行える。

### 環境検出

```bash
# cmux 内かどうかの判定
if [ -n "$CMUX_WORKSPACE_ID" ]; then
  echo "cmux 内で実行中"
fi
```

`CMUX_WORKSPACE_ID` が未設定の場合は cmux コマンドを使わないこと。

### 基本コマンド

```bash
# 現在のコンテキスト確認
cmux identify --json

# ワークスペース・ペイン・サーフェス一覧
cmux list-workspaces
cmux list-panes
cmux list-pane-surfaces --pane pane:1
```

### ペイン分割とコマンド実行

テスト・シミュレーション実行は新規ペインで行い、メインのコンテキストを汚さない。

```bash
# 右にペイン分割してコマンドを送る
cmux new-split right
cmux send --surface surface:2 "python -m pytest tests/ -v 2>&1\n"

# 下にペイン分割
cmux new-split down

# 実行結果を読み取る
cmux read-screen --surface surface:2 --lines 50

# MATLAB 実行
cmux send --surface surface:2 "matlab -batch \"run('src/main_sim.m')\"\n"
```

### 通知とプログレス

長時間タスク（テスト・シミュレーション・ビルド）ではプログレスと通知を活用する。

```bash
# プログレスバー（サイドバーに表示される）
cmux set-progress 0.0 --label "テスト開始"
cmux set-progress 0.5 --label "テスト実行中"
cmux set-progress 1.0 --label "完了"
cmux clear-progress

# 完了通知（macOS デスクトップ通知が飛ぶ）
cmux notify --title "テスト完了" --body "全テスト pass"
```

### 運用ルール

- テスト・シミュレーション実行は必ず別ペインで行う（`cmux new-split` → `cmux send`）
- 実行結果は `cmux read-screen` で確認してからメインペインに戻る
- 長時間タスク開始時は `cmux set-progress`、完了時は `cmux notify` を使う
- 既存のペインを確認（`cmux list-panes`）してから分割する。不要な重複ペインを作らない
- 作業完了後、不要なペインは閉じる
