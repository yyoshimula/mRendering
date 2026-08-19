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

### 実行（GUI）

```bash
# ブラウザ GUI を起動（http://127.0.0.1:8600、Flask 不要・標準ライブラリのみ）
python mrender.py gui
python mrender.py gui --port 8765 --no-browser
```

**リモート実行（DGX Spark）**: 2 方式ある（詳細は
[tools/dgx/README.md](tools/dgx/README.md)）。
① **GUI 内マシン選択（推奨）**: GUI を Mac で動かし、ヘッダーの「実行マシン」
セレクタでジョブ・ライブプレビューごとに local / dgx を切替。リモート定義は
`gui_hosts.json`（ssh ホスト名 / リポジトリ dir / MRENDER_VARIANT。自ホスト名と
同名エントリは自動除外）。リモートジョブは YAML を scp → `ssh -tt` で実行し、
runs/<run> を rsync でローカルへ自動ミラー（進捗・最新フレーム・成果物リンクは
ローカル実行と同一 UI、最終同期中は status=syncing）。ライブプレビュー worker は
`ssh -L`（リモート側ポートは +100 オフセット）で常駐し、プロキシ経路は不変。
事前に `tools/dgx/sync_to_dgx.sh` でコード同期が必要。
② **GUI ごと DGX で動かす**: `tools/dgx/connect_dgx.sh`（tmux 常駐 + SSH トンネル
8600 1 本 + ブラウザ起動）。
Mitsuba バリアントは `MRENDER_VARIANT` 環境変数（カンマ区切り先勝ち、既定
`llvm_ad_rgb`、実装 `mi_variant.py`）で切り替え、DGX では
`cuda_ad_rgb,llvm_ad_rgb`（GPU 優先・CPU フォールバック）を既定にする。

GUI（`gui_server.py` + `gui/index.html`）は verb 選択・プリセット読込・フォーム編集・
YAML プレビュー・プリセット保存・レンダリング開始・進捗/最新フレーム表示・過去ラン
（runs/）一覧までを提供する。ジョブは `runs/_gui_configs/` に YAML を書き出し、
`mrender.py <verb> --config <yaml>` をサブプロセス起動する（output_dir 明示指定で
ランディレクトリを事前確定し、frames/*.png のカウントで進捗率を出す）。
GUI サーバーは Mitsuba を import しない軽量設計。relative/rotation の
フォームデフォルト値は `verb_parsers.py`（Mitsuba 非依存のパーサ定義）から
`parse_args([])` で自動導出するため、argparse 定義の変更に自動追従する
（旧ハードコード辞書は廃止済み）。プリセットのフォーム未対応キーは
extraKeys 機構でそのまま透過適用される（フォーム下部に一覧表示）。

**ライブプレビュー（全 verb 対応）**: 右カラムの「ライブプレビュー ON」で
Blender のレンダープレビュー風のビューポートが使える。
Mitsuba 常駐の `live_worker.py` を gui_server が遅延 spawn（既定ポート =
GUI ポート+1、`--live-port` で変更、ログは `runs/_gui_configs/live_worker.log`）し、
`/api/live/update|frame|status|stop` をプロキシする。フォーム変更は 200ms
デバウンスで再レンダ（~0.3s、480×360/spp4 相当）、アイドル時にフル解像度で
自動リファイン。ビューポート操作: 左ドラッグ=オービット / ホイール=ドリー /
Shift+ドラッグ=パン（結果は camera_origin/target フォーム欄へ書き戻し =
フォームが単一の真実源）。タイムラインスライダーで tumble/csv の姿勢を
スクラブ（軌跡は worker 側で事前計算キャッシュ）。太陽ウィジェットは verb 別:
relative=方位/仰角⇄sun_direction、render 系=sun_angle スライダー（空欄=天文学的
自動計算、sun_rotate ON 時は無効）、rotation=非表示。
verb 別のシーン再現: relative は `relative_motion.py`、rotation は
`simple_rotation.py`、render/preview/onboard/lightcurve は `satellite_orbit.py`
のフレームロジック（トーンマップ経路 apply_tonemap + srgb_gamma=False 含む）を
再利用しており、リファイン画像は各 verb の本番出力とピクセル一致
（render 系はビット一致、relative/rotation は Mitsuba 自体の ±1LSB 揺らぎのみ）。
render 系のカメラ操作は view_mode が解決した実カメラ（/api/live/status の
camera）をシードに camera_origin/target オーバーライド欄へ書き戻す。
lightcurve のライブ表示はシーン画像のみ（CSV は本番実行で出力）。
プレビュー高速化: GLB→OBJ 変換・OBJ パーツ分割・mi.Bitmap を (path,mtime)
キャッシュ、プレビューパスのみ envmap/大型テクスチャを 2048px にダウンサンプル
（`PREVIEW_ENV_MAX_WIDTH`、リファインは原寸）。ロード済み Emitter 等の
シーンオブジェクト共有は画が壊れるため禁止（Bitmap 共有は安全）。
**操作中の簡易描画**: ドラッグ/ホイール/タイムラインスクラブ・再生中は
`quality.interactive: true` が送られ、worker が簡易化プロファイル
（大気・雲・夜光・starfield を一時オフ、integrator を path/max_depth=2 に
差し替え、解像度さらに 1/2・spp≤2。`simplify_fields` / `simplify_scene`）で
~0.1-0.2 s 応答にする。手を離すと通常品質 1 発 → 自動リファイン
（リファインは常にフル品質で、簡易化は漏れない — ビット一致検証済み）。
ステータス行に「簡易」バッジ（`X-Live-Simplified` ヘッダ、gui_server の
_LIVE_HEADERS 許可リストに登録済み）。「簡易操作」チェックで無効化可。
注意: 各 verb の argparse（verb_parsers.py）を変えると live_worker・GUI とも
`build_parser().parse_args([])` 由来なので自動追従する。
既知の微小差: relative の starfield envmap の最輝星 数 px が live⇔バッチ間で
≤ 十数 LSB ずれることがある（Bitmap キャッシュ経路由来。ターゲット・地球は
ビット一致）。

**AI アシスタント drawer（全 verb 共通）**: ヘッダーの「🤖 AI」で右ドロワーが開く。
バックエンドはローカルの `claude` CLI（Claude 系）/ `cursor-agent`（Grok 系）を
ヘッドレス起動して stream-json を SSE 中継する monet（llmWiki）方式。API キー不要。
`POST /api/ai`（prompt / session_id / model / effort / gui_context）、
`POST /api/ai/stop`、`GET /api/ai/defaults`。localhost バインド時のみ有効。
claude は `--permission-mode acceptEdits`（全許可フラグは不使用）、grok は
`--trust`（このリポジトリのみ信頼）+ モデル ID は `cursor-grok-4.6-<effort>[-fast]`
形式へ `cursor_model_id()` で合成（--effort フラグは無い）。
AI が presets/*.yaml を編集すると done イベントの edited_files 経由で GUI が
プリセット一覧を更新し、読み込み中プリセットなら自動再読込→ライブプレビュー反映。
「現在の GUI 設定を渡す」ON で現在フォームの YAML が文脈として注入される。

### 実行（mrender.py: 統合 CLI）

**重要**: `render` / `lightcurve` / `preview` / `onboard` 系のパラメータ（軌道要素、フレーム数、サンプル数、外部モデル指定など）は **YAML プリセット経由** で指定する。`config_loader.build_parser()` は `--config` 以外の個別 CLI フラグを登録しない設計なので、`--altitude` や `--frames` を直接渡すことはできない。CLI で上書きしたい場合は、差分を別 YAML に書いて `--config A.yaml B.yaml`（後勝ち）で重ねる。

```bash
# 軌道レンダリング（render verb）
python mrender.py render --config presets/iss.yaml

# 複数 YAML 合成（後の指定が優先）
python mrender.py render --config presets/iss.yaml presets/earth_beauty.yaml

# CSV ephemeris から軌道・姿勢を再生
python mrender.py render --config presets/csv_attitude_orbit.yaml

# 1 フレームだけプレビュー（材質・ライティング調整用）
python mrender.py preview --config presets/iss.yaml

# 観測者から見たライトカーブのみ高速計算
python mrender.py lightcurve --config presets/iss.yaml

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

### 実行（On-Orbit Servicing 近接撮像シナリオ）

```bash
# サービサ機載カメラ（chief、非描画）から 35 m 先のタンブリング Hubble を撮像。
# 地球背景・太陽黒体色・星空 envmap 付きのフォトリアリスティック設定（状態推定研究向け）
python mrender.py relative --config presets/oos_hubble.yaml

# 低品質プレビュー（CLI 上書き）
python mrender.py relative --config presets/oos_hubble.yaml --frames 3 --samples 16
```

relative verb のフォトリアル環境オプション（`relative_motion.py`）:
`--hide-chief`（カメラ=機載視点で chief 非描画）/ `--show-earth` +
`--earth-direction --earth-altitude-km --earth-texture --earth-rotation-deg`
（km 単位シーンに地球球体を背景配置、earthshine の照り返しはパストレで自動）/
`--sun-direction --sun-irradiance --sun-temperature`（黒体放射色）/
`--starfield <exr>` + `--env-brightness` / `--camera-up` / `--max-depth`。

**近接シーンは relative verb を使うこと**: render/onboard パス（地球半径=1.0 の
シーン単位）では数十 m の分離距離が float32 精度で崩れる。relative は km 単位
シーンなので近接撮像に強い。

`models/hubble.obj` は NASA 3D Resources の "Hubble Space Telescope (A)"（Draco
圧縮 glb、`models/hubble_a.glb`）を Blender ヘッドレスで OBJ + PNG テクスチャ
（`models/hubble_textures/`）に変換したもの。実寸 13.2 m に正規化、原点=バウン
ディングボックス中心、パーツ名（OBJ の 'o'）はマテリアル別に hbltel_1..4 /
hbltel_wfc_1。パーツ↔テクスチャ対応は `models/hubble_textures/material_map.txt`。

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
- **mrender.py** - 統合 CLI（render / lightcurve / preview / rotation / relative / onboard / gui）
- **satellite_orbit.py** - render/lightcurve/preview のコア：軌道計算、シーン生成、レンダリングループ
- **simple_rotation.py** - rotation のコア：オイラー回転運動方程式、クォータニオン姿勢、OBJパーツ別BSDF（軌道なし）
- **relative_motion.py** - relative のコア：相対位置・相対姿勢で 2 機を配置（軌道なし、static/csv/tumble モード、地球背景・太陽・星空のフォトリアル環境オプション付き）
- **gui_server.py / gui/index.html** - ブラウザ GUI（標準ライブラリ HTTP サーバー + 単一 HTML）。設定フォーム→YAML 生成→verb サブプロセス起動→進捗表示
- **verbs/** - mrender サブコマンドのシム
- **scene_common.py** - relative/rotation 共有のシーン部品・姿勢伝播（propagate_attitude / create_axes / create_satellite / split_obj_by_parts〔(path,mtime) キャッシュ内蔵〕/ load_model_with_parts）。**唯一の実装**で、両 verb は再エクスポート
- **verb_parsers.py** - relative/rotation の argparse 定義（Mitsuba 非依存）。GUI・live_worker のデフォルト自動導出の真実源
- **asset_cache.py** - mi.Bitmap / GLB→OBJ 変換 / BSDF 用 BitmapTexture 実体の (path,mtime) キャッシュ。バッチとライブの両経路が使用。**Emitter の実体共有は禁止**（画が壊れる。BSDF テクスチャ実体と Bitmap は共有安全・ゴールデン検証済み）
- **mi_variant.py** - Mitsuba バリアント選択の唯一の実装（環境変数 `MRENDER_VARIANT`、既定 llvm_ad_rgb。DGX/CUDA 切替用）
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
- YAMLキー名はargparse dest名と一致させる（例: `model_path`, `camera_origin`）。
  **未知キーは黙って無視される**ので、効かない設定があったらまずキー名を疑う。
  地球テクスチャの正キーは `earth_day_texture` / `earth_night_texture` /
  `earth_cloud_texture`。旧キー（`earth:` セクション配下の `day_texture` 等）は
  render 系 CLI では config_loader が別名変換するが、GUI・ライブプレビューの
  素朴フラット化経路では変換されないため、正キーで書くのが安全
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
- バリアント：`llvm_ad_rgb`（LLVM JIT による CPU パストレーシング）。GPU を使う場合は各モジュール冒頭の `mi.set_variant()` を `cuda_ad_rgb` に変更する
- デフォルト：128サンプル/ピクセル、12バウンス深度
- 出力：`mrender.py` 経由なら `runs/<ts>_<verb>_<name>/frames/` に PNG 連番（`output/` はスタンドアロン実行時の既定で、`simple_rotation.py` は `output/simple/`、`relative_motion.py` は `output/relative/`）

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
