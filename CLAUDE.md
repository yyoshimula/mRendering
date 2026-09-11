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
セレクタでジョブ・ライブプレビューごとに local / リモート GPU を切替。リモート定義は
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

**ライブプレビュー（全 verb 対応。groundobs は画像のみ・カメラ操作無効）**: 右カラムの「ライブプレビュー ON」で
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

**Mitsuba 型の注釈は import 時に評価させない**: `-> mi.ScalarTransform4f` のような
戻り値注釈はバリアント未設定の段階で import されると AttributeError になる
（satellite_orbit → scene_builder → scene_earth の連鎖で render 系 4 verb が CLI/GUI から
起動不能になった 2026-09-08 の回帰）。Mitsuba を使うモジュールには
`from __future__ import annotations` を置く。CLI 起動経路は `tests/test_cli_import_order.py`
がサブプロセスで検証する（テスト本体は scene_common を先に import するので検出できない）。

**座標系と時刻（全 verb 共通）**: 「ECI」は **平均赤道・平均分点 of date**。
地球自転は GMST（IAU 1982）、TLE/SGP4 の TEME もこの系に近い。太陽（VSOP87）と
恒星（Hipparcos ICRS≈J2000）は `orbit_mechanics.precession_j2000_to_date()` で
of-date へ歳差補正する（2026 年で J2000 との差 0.37° = 22′、狭視野の星野では
無視できない）。yoshimulib `sun_lon_lat_r` は docstring に反して J2000 黄経を返す
（同ライブラリの `sun()` はそれを前提に整合）ので、of-date が必要なら
`orbit_mechanics.sun_position_eci_km(jd)` を使うこと。
render/preview/lightcurve/onboard も `epoch_utc`（ISO 8601、`rendering:` 等に置く
フラットキー）を指定すると太陽 = VSOP87、自転角 = GMST の実時刻になる。未指定時は
従来の簡易モデル（t=0 で太陽 = +x = 春分点、グリニッジ = +x、太陽は 365.25 日の
円運動）。`sun_angle` は太陽の **黄経** [deg]（黄緯 0、黄道傾斜 23.44°）。
lightcurve の観測者はフレームごとに同じ自転角で回し、`light_curve_fov` は
null（既定）で物体の見かけサイズから自動決定する（既定 `satellite_scale` 0.002 は
地球半径単位で数十 km 級なので固定 2° では全距離でクリップしていた）。
黒体色 `blackbody_to_rgb` は Planck × CIE 1931 → **線形** sRGB（最大成分 1、
5778 K ≈ (1, 0.88, 0.82)）。

### 実行（rotation: 単機タンブリング、軌道なし）

`rotation` verb (`simple_rotation.py`) は専用の CLI フラグを持つ（`--frames`, `--samples`, `--Ix`, `--wx`, `--model-path`, `--model-scale`, `--camera-origin` など）。YAML と CLI を混在可。

```bash
# mrender 経由（推奨、runs/ に自動配置）
python mrender.py rotation --config presets/rotation_hubble.yaml

# スタンドアロン
python simple_rotation.py --config presets/rotation_hubble.yaml
python simple_rotation.py --config presets/rotation_hubble.yaml --frames 10 --samples 4
python simple_rotation.py --frames 30
python simple_rotation.py --model-path models/hubble.obj --model-scale 0.1 --frames 10
```

### relative の高速化（永続シーン / フレーム並列）

relative はフレームごとの `mi.load_dict`（シーンパース・BVH・カーネル再トレース）
が CPU 支配的で、GPU バリアントでは GPU 使用率が数 % に張り付く。対策 2 段:

- **永続シーン（既定 ON、`--no-persistent-scene` で従来動作）**: `PersistentScene`
  が前フレーム scene_dict との差分を取り、mi.traverse で更新可能なものだけ
  書き換えて再レンダ。対応: メッシュ to_world（頂点/法線焼き直し。**法線は
  positions 更新→update→normals 更新→update の 2 段**。同一 update だと Mesh が
  法線を自動再計算して上書きする）/ 解析形状・envmap の to_world（**sphere は
  明示 radius を to_world に焼き込むので scale(radius) を合成**）/ directional の
  direction・irradiance（to_world の +z 列。他 2 列は観測量に無関係）/ bitmap
  差し替え（クロップ再センタ。**データ内部表現はバリアント依存**〔llvm=リニア
  Float32、cuda=生値+サンプル時変換〕なので使い捨て bitmap プラグインに
  読ませて data テンソルを写す。to_uv は UV 空間の 3×3 AffineTransform3f）。
  未対応の差分（キー集合変化・BSDF 変更等）は自動で作り直しにフォールバック。
  検証: llvm/cuda とも static/tumble/absolute で作り直しパスと ≤1 LSB 一致。
  実測: absolute 640×480/256spp が Mac llvm 3.3→1.5 s/frame、DGX cuda
  2.2→1.35 s/frame（以後は GPU レンダ自体が支配的）。
- **`--jobs N`（GUI: 並列プロセス数）**: フレーム範囲を N 分割してサブプロセス
  並列。tumble はチャンク先頭まで決定論的に姿勢を前進させるので直列と
  ±1LSB（Mitsuba 固有の揺らぎ）以内で一致。**多フレーム × CPU(llvm) 向け**。
  DGX cuda では永続シーン後は GPU 律速のため効果薄、少フレームでは
  プロセス起動 ~10s が勝って逆効果。チャンク子プロセスは
  `relative_motion.py --args-file <json>`（解決済み Namespace を JSON 渡し）。
- **absolute の地球は UV 球メッシュが既定**（`_uv_sphere_obj`、512×256 分割・
  解析 sphere と同一 UV 規約、`--earth-analytic-sphere` で従来へ）: 解析
  sphere の to_world はスカラー状態のため毎フレーム更新が Dr.Jit カーネルへの
  リテラル焼き込み→全再コンパイル（cuda 実測 ~2.7 s/frame）を誘発する。
  メッシュなら PersistentScene の頂点配列更新に乗り再コンパイルなし。解析
  sphere との画差は max 6 LSB / 平均 0.2 LSB 以下（テッセレーション+法線補間分）。
- **クロップ再センタのスパイク対策**: サブタイル JPEG デコードをスレッド並列
  （PIL は C 層で GIL を離す）、crop PNG は compress_level=1（ロスレス維持）、
  縮小は BOX（LANCZOS は 12000px 級で数秒）。スパイク 9→2.8 s（~14 フレームに
  1 回）。`MRENDER_TIMING=1` で工程別タイマー（env/state / dict / pscene 内訳
  diff/apply/render / write）が stderr に出る。
  最終実測（absolute 640×480 cuda）: 定常 **0.25 s/frame**（元 2.2 の ~9 倍）、
  20 フレーム通し 38→19.5 s、1024spp でも 1.5 s/frame。

### 太陽電池パネルの入射照度・発電量（PV 計測、`pv_irradiance.py`）

**relative（static/csv/tumble/absolute）と groundobs** でパネルを 1 枚でも指定すると、
フレームごとに受光面の放射照度を **直達 / 地球照 / その他** に分けて
`runs/<run>/pv_irradiance.csv` に書く（画像レンダと同時。列: frame, t_s, panel,
host, area_m2, nu, cos_sun, sun_visible, e_direct_wm2, e_earth_wm2, e_other_wm2,
e_total_wm2, p_w, e_earth_{r,g,b}_wm2）。発電量は P = η·A·E_total
（`pv_efficiency`、既定 0.30）。フラグは `verb_parsers.add_pv_arguments` で両 verb 共通
（host は relative = deputy|chief、groundobs = target）。GUI は両 verb に
「太陽電池パネル」セクション（`gui_server.pv_section`）。

```bash
# Hubble 軌道 1 周・太陽指向・仮想パネル 2 翼 + 反太陽セル（一様アルベド 0.3 の地球照）
python mrender.py relative --config presets/pv_hubble_orbit.yaml
# BMNG テクスチャ地球 / 当日の実写雲分布（GIBS）で計算（地球照は下限値、後述）
python mrender.py relative --config presets/pv_hubble_orbit.yaml --earth-uniform-albedo ''  # YAML で空欄に
python mrender.py relative --config presets/pv_hubble_orbit.yaml --earth-gibs
# groundobs（地上観測 + 軌道上の PV）: 観測像は従来どおり、PV は軌道・姿勢・GMST 地球から計算
python mrender.py groundobs --config presets/groundobs_hubble.yaml \
    --pv-panel-size 2.5 7.1 --pv-panel-center 3.9 0 -0.8 --pv-panel-normal 0 0 -1
# 解析参照（Mitsuba 不要）: 単一 BRDF 球の地球照を格子積分
python tools/pv_earthshine_reference.py --altitude-km 550 --sun-zenith-deg 0 60
```

- **パネル指定**: YAML `pv_panels:`（list[dict]: name / host / center [m 機体系] /
  normal / up / size [m] / efficiency / bsdf）で仮想矩形パネル（片面受光、画にも
  出る）、`pv_parts:` で OBJ パーツ全体を受光面に（閉じたメッシュは表裏全フェイス
  平均になるので薄板は半分程度に出る）、CLI 単一パネルは `--pv-panel-size W H`
  + `--pv-panel-center/normal/up`。手続き生成の既定衛星は km 級なので、その内部に
  パネルを置くと全遮蔽で 0 になる（実モデルか機体外の位置を使うこと）。
- **仕組み**: 間接光は Mitsuba `irradiancemeter`（シェイプ入れ子、1×1 film に
  ∫L cosθ dω がそのまま出る）。**directional 太陽は irradiancemeter に写らない**
  （センサ位置で NEE が走らない、実測 0）ので直達は解析: E_direct =
  S0·ν·⟨max(0,n·ŝ)·vis⟩、vis は `scene.ray_test` による自己遮蔽（地球なしシーン
  で判定。食は ν で扱う）。地球照は「地球を反射率 0 の黒体にした同一シーン」との
  差分（地球を外すと地球に隠れるはずの環境光が下半球から入り −6% になる）。
  **PV シーンの太陽には食 ν を掛けない**（`pv_scene_dicts(sun_rgb=...)`。衛星が
  影でも地球の昼側は照らされている。機体相互反射はシーン内の地球球体が幾何学的に
  遮る = 半影は硬い影近似）。環境光は既定で計測から除外（既定フィル光 0.02 は
  可視化用 ≈ 18 W/m²。`--pv-include-env` で含める）。
  Mitsuba の辞書パーサは内容同一ノードを 1 オブジェクトに畳むため、センサは
  パネルごとに sampler seed を変えて一意化している（でないと 2 枚目で
  "endpoint can only be attached to a single shape"）。
- **groundobs の地球**: シーンに地球が無いので `earth_backdrop_at` が物体原点の
  ECI 系に地球球体（既定 `earth_uniform_albedo` 0.3、`--earth-texture` で
  テクスチャ + GMST 姿勢 + 可視域クロップ）を置く。`--earthshine` を立てると
  観測像のレンダにも地球を入れ、物体への照り返しが測光に乗る（既定 OFF。
  ランバート球の解析検証は太陽のみで成立。カメラ far_clip は代理距離基準なので
  地球は画に写らず、物体→地球→太陽のパスだけが乗る）。
- **単位換算**: E_phys = S0_wm2 · lum(E_rgb)/lum(sun_rgb)（Rec.709 輝度、groundobs と
  同じ規約）。RGB 3 帯域の可視近似で、NIR 域（植生 0.4〜0.5、海 ≈ 0）は含まれない
  → Si/3 接合セルの応答域に対して地球照は過小になりうる（spectral バリアントは
  将来課題）。`pv_sun_irradiance_wm2` はレンダ単位 `sun_irradiance` とは独立。
- **検証済み**: 一様ランバート球（0.3、550 km）で Mitsuba 計測 vs 格子積分
  （`tools/pv_earthshine_reference.py`、Fankhauser+2023 Lumos 由来）が太陽直下
  343 W/m²・天頂角 60° で 172 W/m² と 0.3% 以内で一致、簡易式 a·S0·(R/(R+h))²
  = 346 は縁の減光ぶん 1% 過大。直達は cos30°×遮蔽 50% を 1% 以内で再現
  （`tests/test_pv_irradiance.py`）。`--jobs N` はチャンク CSV を親が結合する。
- **地球モデルで地球照は桁で変わる**: BMNG テクスチャ地球は雲なし合成画像を
  sRGB→リニア変換した反射率（海 ~0.02、陸 ~0.1）なので地球照は下限値
  （同一姿勢・時刻で一様 0.3 球 185 W/m² に対し BMNG 5.5 W/m²）。エネルギー収支
  目的では `earth_uniform_albedo: 0.3`（雲込み全球平均、CERES）を使う。GIBS
  実写は当日の雲を含むが表示用画像で放射量校正はない。文献上、惑星アルベドの
  約 9 割は雲（Donohoe & Battisti 2011）で、地表の海陸差は 1 割程度。
- 地球の熱赤外（~237 W/m²）は PV には効かないので対象外。海面サングリント等の
  非ランバート BRDF は未対応（地球は diffuse）。CARS 実測の Phong フィットは
  参照ツールに定数として収録（単一波長・大気込みなのでアルベドとして使わない）。

### 実行（relative: 2 機の相対配置 + 絶対軌道モード）

`relative` verb (`relative_motion.py`) も専用 CLI フラグを持つ（`--mode`, `--rel-position`, `--rel-quat`, `--rel-csv`, `--frames`, `--wx` など）。

```bash
# mrender 経由（推奨、runs/ に自動配置）
python mrender.py relative --config presets/relative_static.yaml

# スタンドアロン: static / csv / tumble モード
python relative_motion.py --frames 30 --rel-position 1.5 0 0
python relative_motion.py --mode csv --rel-csv input/rel_state_hcw.csv --frames 60
python relative_motion.py --mode tumble --rel-position 1.5 0 0 --wx 0.3 --wz 1.0

# absolute モード: 絶対軌道 2 本 → 相対状態 + フル物理環境を導出
python mrender.py relative --config <absolute モードの YAML>   # 研究室内の例は internal/presets/
```

**mode=absolute（`AbsoluteOrbitContext`）**: chief/deputy の絶対軌道
（`--chief-oe/--deputy-oe` = [a_km, e, i_deg, Ω_deg, ω_deg, M0_deg]、または
`--chief-orbit-csv/--deputy-orbit-csv` = CsvEphemeris スキーマ [rad]）を伝播し、
chief の RTN(LVLH) に落として相対状態を作る（シーン = chief 中心 RTN [km]、
x=R, y=T, z=N。camera_* もこの回転系で指定）。環境は `--epoch-utc` 基準の実時刻で
毎フレーム更新: 太陽方向 = VSOP87（`orbit_mechanics.sun_position_eci_km`、平均分点 of date）、
食 = yoshimulib `shadow()` の ν を照度に乗算、地球姿勢 = GMST + フル 3 軸
`orientation`（`create_earth_backdrop(orientation=...)`。直下点が地上軌跡どおりに
動き、可視域クロップを毎フレーム再計算 = フレームあたり数百 ms のタイル
デコードが乗る）、星空 envmap = to_world で ECI 固定（恒星がシーンの回転に
伴い流れる）。`sun_direction / earth_direction / earth_altitude_km /
earth_rotation_deg` は absolute では無視（軌道から自動計算）。deputy 姿勢は
`--deputy-attitude`: tumble（ECI 系トルクフリー伝播。rel_quat は t=0 の RTN
相対初期姿勢）/ lvlh（deputy 自身の RTN + rel_quat 固定オフセット）/
csv（deputy 軌道 CSV の q1..q4 = ECI→Body）。ライブプレビューも
live_worker が同じ `AbsoluteOrbitContext` を共用して対応済み。

### 実行（groundobs: 地上望遠鏡からの光学観測）

`groundobs` verb (`ground_observation.py`) は地上局 (lat/lon/alt) から軌道上物体を
光学望遠鏡で観測したときの**見かけ等級ライトカーブ** (observation.csv) と
**望遠鏡センサ像**（小物体=シーイング円盤の点像、大物体=分解像。同一パイプライン）
を出力する。専用 CLI フラグを持つ（`verb_parsers.build_groundobs_parser`、YAML 混在可）。

```bash
# LEO 大型物体（Hubble 13.2 m）の天頂パス: 分解像 + タンブル光度変調
python mrender.py groundobs --config presets/groundobs_hubble.yaml

# GEO 小物体（1 m 球）: 点像 + CCD ノイズ（~13 mag）
python mrender.py groundobs --config presets/groundobs_geo_point.yaml

# GEO サーベイ: 恒星時追尾・広視野・恒星背景・物体ストリーク（SSA 模擬画像）
python mrender.py groundobs --config presets/groundobs_geo_survey.yaml

# TLE 入力（SGP4 伝播。epoch 未指定時は TLE エポックを t=0 に採用）
python mrender.py groundobs --config presets/groundobs_hubble.yaml   # に --tle を足すか
python ground_observation.py --tle input/iss_sample.tle --frames 10
```

物理モデルの要点:
- **地球自転あり**: 観測地は WGS-84 ECEF → GMST で ECI 回転。UTC エポック
  (`epoch_utc`) 基準の実時刻で、太陽位置は yoshimulib VSOP87
  （`sun_moon.ephemeris.sun_lon_lat_r` を直接使用。`ephemeris.sun()` 本体は
  `au2km(sun_au, const)` の引数バグで TypeError になるため未使用）
- **絶対測光**: 太陽 directional emitter に太陽定数 S0 [W/m²] を与え、リニア
  レンダ画像の立体角積分で開口面照度 E → `m = −26.74 − 2.5·log10(E/S0)`。
  ランバート球の解析解と 0.1%（0.001 mag）一致を検証済み
- **本影/半影**: yoshimulib `shadow()` の照射率 ν をそのまま太陽照度に乗算
- **大気**: 減光 k·X（平面平行 airmass）+ シーイング（ガウス PSF、FWHM 指定）
  + 屈折（Sæmundsson 式。airmass・可視判定は視仰角 el_app で評価、
  `--no-refraction` で無効化）
- **大気揺らぎ**（`--turbulence`）: ガウスシーイングの代わりに Kolmogorov
  位相スクリーン（FFT 法 + 3 レベル・サブハーモニクス、von Kármán 外部スケール
  L0）から瞬時 PSF = |FFT{P·e^{iφ}}|² を生成して畳み込む。スペックル +
  フレーム間の像揺れ（tip-tilt）+ 露光平均（サブ露光数 = exposure_s/τ0、
  `--turbulence-max-screens` 上限）を物理再現し、長露光ではシーイング円盤に
  収束する。r0 は `seeing_arcsec` から換算（FWHM = 0.98λ/r0）、カーネル総和 1
  正規化で測光値はガウス経路とビット一致。短露光（≲0.1 s）の分解像形状推定
  向け（例 `presets/groundobs_hubble_turbulence.yaml`）。検証は
  `internal/groundobs_validation/turbulence_validation.py`（構造関数 ±10%・
  長露光 FWHM 誤差 4%・flux 保存を確認済み）
- **軌道入力 3 系統**: `--tle`（SGP4、要 `pip install sgp4`。TEME≈ECI 近似、
  epoch 未指定時は TLE エポック採用）> `--orbit-csv` > ケプラー要素
- **恒星背景**（`--show-stars`）: Hipparcos npz（`assets/hipparcos.npz`、
  SatCap 由来、固有運動込み 118k 星）を物体レンダと同一のカメラ基底で投影。
  Mitsuba look_at の画像座標系は実測で確定済み（列+ = f×up、行+ = −up。
  `camera_basis` / `project_to_grid`）
- **追尾モード**（`--tracking-mode`）: target=物体追尾（恒星が露光中に
  トレイルを引く）/ sidereal=恒星時追尾（恒星は点像、物体が shift-and-add で
  ストリーク化）。トレイル/ストリーク長は `--exposure-s` で決まる
- **float32 対策(重要)**: カメラは真レンジではなく代理距離
  d_r = min(range, 1000×バウンディング半径) に置く。真距離だと GEO で
  レイ交差の判別式が桁落ちする。directional 光源のみなので結果は d_r に不変
- `--start-overhead` で Ω(RAAN)・M₀ を自動調整し t=0 に天頂パスを作れる（デモ用）
- センサノイズ（`--sensor-noise`）: 口径・QE・露光から光電子数 → ショット +
  読み出しノイズ + 夜空背景 [mag/arcsec²]（SatCap の CCD 式を物理単位で移植）。
  光電子換算は **V バンド**（V=0 で 8.84e9 photons/s/m²、Bessell）: 等級→照度の
  ボロメトリック換算値 E を 550 nm 光子で割ると全波長 1361 W/m² 分を数えて ≈8.6 倍
  過大になるので、`photons_per_wm2()` で帯域内（太陽 ≈159 W/m²）に換算する。
  表示 PNG は背景中央値を引き 8σ フロアでストレッチ（実データは *_linear.npy）。
  ノイズ OFF でもモノクロ検出器を模すなら `--monochrome`（Rec.709 輝度、
  *_linear.npy も 2D になる。sensor-noise ON は常にモノクロなので不要）
- **PV 計測 / 地球照**（`--pv-*`、`--earthshine`、`--earth-uniform-albedo`、
  `--earth-texture`）: 「太陽電池パネルの入射照度・発電量」節を参照。PV は観測者の
  可視性と無関係に全フレーム計測し、`pv_irradiance.csv` を observation.csv の隣に書く
- **ライブプレビュー対応**: `build_context` + `render_observation_frame` を
  live_worker が共用し、処理済み画像を `FrameBuild.image01` で返す特殊経路
  （worker の mi.render はスキップ）。カメラは観測幾何から決まるため
  ビューポートのカメラ操作は無効（フロントの `LIVE_STATIC_CAMERA_VERBS`）。
  タイムラインスクラブ・フォーム変更再レンダは他 verb と同様

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
（km 単位シーンに地球球体を背景配置、earthshine の照り返しはパストレで自動）。
**地球の可視域クロップ（既定 ON）**: LEO では地平線キャップ（400 km で中心角
~20°）しか見えないため、chief 直下点まわりのキャップ + 余白だけを高解像度
ソースから切り出し、球の追加自転 + bitmap `to_uv` でクロップ範囲に
再マップする（見た目の地理は不変・実効解像度のみ向上。低解像度全球貼りとの
UV 整合はゴールデン検証済みで最大 1 LSB）。ソースは `--earth-highres-texture`
（既定 `assets/textures/earth_day_500m` = NASA BMNG 500 m/px 相当 86400×43200 を
2700px 角 512 枚に分割したタイル群。`tools/prepare_bmng.py` で生成、~420 MB。
可視域にかかるサブタイルだけデコードするので軽い）。無ければ
`assets/textures/earth_day.jpg`（21600×10800）→ `earth_texture` の順に
フォールバック。`--no-earth-crop` で従来動作、`--earth-crop-margin-deg`
（既定 5°）で余白、`--earth-crop-max-dim`（既定 8192、500 m/px をフルに使う
なら 12000）でクロップ上限。Mitsuba sphere の UV 規約は u=atan2(y,x)/2π,
v=acos(z)/π（実測確定）。クロップはプロセス内キャッシュでライブプレビューの
スクラブ中も再計算されない。
**GIBS 実写モード**: `--earth-gibs` で NASA GIBS (WMTS/EPSG:4326) から可視域
タイルだけをオンデマンド取得（既定レイヤ MODIS Terra TrueColor 250 m/px、
実写日次・雲込み。`--earth-gibs-date YYYY-MM-DD`、未指定は昨日 UTC。
`--earth-gibs-layer` で Aqua/VIIRS 等に変更可）。ズームレベルは可視域の必要
画素数と crop_max_dim から自動選択、タイルは `runs/_gibs_cache/` に永続
キャッシュ（rsync 対象外・GUI 一覧非表示）、ネットワーク失敗時は BMNG →
earth_texture へ自動フォールバック。要出典表記: NASA GIBS。/
`--sun-direction --sun-irradiance --sun-temperature`（黒体放射色）/
`--starfield <exr>` + `--env-brightness` / `--camera-up` / `--max-depth`。
**星空 envmap の規約**: `assets/starfield.exr` は「内側から見た星図」
（u = 1 − RA/2π、v = 1/2 − Dec/π）。Mitsuba envmap のローカル座標は
u = atan2(x, −z)/2π・極 = ±y（左手回り）なので、RA を u に直接写すと鏡像
（行列式 −1）になり回転では直せない。consumers（scene_builder / relative_motion）は
`optical_lighting.STARFIELD_LOCAL_TO_ECI`（local +y→ECI +z、local −z→ECI +x）を
to_world に合成する（absolute では A_i2rtn を左から掛ける）。2026-09-08 に規約を
修正したので、それ以前に生成した EXR は `tools/generate_starfield.py --width 8192
--milky-way --output assets/starfield.exr` で再生成が必要。

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
- **ground_observation.py** - groundobs のコア：地上望遠鏡観測（WGS-84 地上局 + GMST + VSOP87 太陽 + TLE/SGP4 + 絶対測光 + 屈折 + シーイング PSF + 恒星背景 + 追尾モード + CCD ノイズ。build_context / render_observation_frame を live_worker と共用）
- **pv_irradiance.py** - relative / groundobs の PV 計測：受光面（仮想矩形 / OBJ パーツ）の直達（解析 + レイキャスト遮蔽）・地球照（irradiancemeter、地球黒体差分）・発電量を CSV 出力。解析参照は `tools/pv_earthshine_reference.py`（Lumos 由来の格子積分）
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
