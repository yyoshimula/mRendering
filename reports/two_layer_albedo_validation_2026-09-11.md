# 2 層アルベドマップ（地表 + 雲）の実機検証レポート

- 日付: 2026-09-11
- 対象: `earth_albedo_map.py`（NASA GIBS の MODIS 雲分率・雲光学的厚さ + 地表アルベド → リニア 16-bit equirect マップ）と、それを地球照光源として使う `relative` verb の PV 計測
- ブランチ: `claude/two-layer-model-validation-3f9505`（PR #2 マージ後 `87e64a5` からの差分）
- 実行した検証コマンド:

```bash
python earth_albedo_map.py --list-layers Cloud Albedo
python earth_albedo_map.py --date 2026-03-20
python mrender.py relative --config presets/pv_hubble_orbit.yaml --earth-albedo-gibs
```

## 1. 結論

- PR #2 時点のコードは GIBS がモックでしか検証されておらず、実機では **雲レイヤが一切取得できなかった**（stats の `cloud_source: none`）。原因は 3 つの GIBS 規約の取り違えで、すべて修正した（§2）。
- 修正後、2026-03-20 の全球マップが雲込みで生成できる。全球平均アルベドは **面積平均 0.377 / 日射量重み平均 0.343**。CERES の惑星アルベド 0.29〜0.30 より 1 割強高く、**上限側の見積り**として扱う（§4）。
- マップを貼った地球の Mitsuba 地球照計測は、独立の格子積分と **1.3% 以内**で一致した。マップの経度向き・GMST 自転・raw bitmap 経路は正しい（§5）。
- `presets/pv_hubble_orbit.yaml` の 90 フレーム本番ランも完走し、`pv_irradiance.csv` を出力した（§6）。

## 2. GIBS 取得の不具合と修正

`--list-layers Cloud Albedo` は最初から通り、GIBS には到達できていた。`--date 2026-03-20` で雲レイヤ取得が失敗した原因は次の 3 点。

| # | 症状 | 原因 | 修正 |
|---|---|---|---|
| 1 | カラーマップ XML が 404 | カラーマップ id はレイヤ id と**別名**（`MODIS_Terra_Cloud_Fraction_Day` → `colormaps/v1.3/MODIS_Cloud_Fraction.xml`、光学的厚さ → `MODIS_VIIRS_Cloud_Optical_Thickness.xml`） | GetCapabilities の `ows:Metadata xlink:role=…colormap/1.3` から URL を解決（`gibs_layer_info`）。GetCapabilities は `runs/_gibs_cache/WMTSCapabilities.xml` に永続キャッシュ |
| 2 | 光学的厚さのタイルが 400 | TileMatrixSet がレイヤごとに違う（雲分率 `2km`、光学的厚さ `1km`、MCD43 アルベド `500m`）。既定は一律 `2km` だった | GetCapabilities から自動解決（`--*-tms` 既定 None。指定がレイヤに無ければ差し替えて警告）。オフライン時は雲 `2km` / τ `1km` / 地表 `500m` にフォールバック |
| 3 | 32 タイル中 17 が 400 | EPSG:4326 のタイル格子は 2 の冪ではない。level 0 = 640×320 px（0.5625°/px、2×1 タイル）、level 2 = 2560×1280 px（5×3 タイル、最下段は半分だけ有効）。コードは 1024×512·2^z（level 2 = 8×4）を仮定していた | `gibs_grid_size` を 640·2^z × 320·2^z に、タイル数を ceil に変更。真色経路（`relative_motion._read_gibs_region`、level 8 = 163840 px）と同一規約になった |

付随して、MCD43 アルベドレイヤ（`MODIS_Combined_L3_White_Sky_Albedo_Daily`）の値は ×1000 の整数（0〜750、fill は 16758）なので `/1000` して 1 超を欠損扱いにする処理を追加した（`--surface-source gibs` 経路。今回の本番では未使用）。

## 3. 生成結果の診断とモデル改良

修正直後の生成結果（改良前）は面積平均 0.379、雲分率 0.657 だった。内訳を診断したところ 3 つの問題があり、モデルを改良した。

### 3.1 τ 未取得の雲画素が定数 0.55 で埋まっていた

雲分率 > 0 の画素のうち、光学的厚さ τ が取得できているのは雲分率重みで **61%** に過ぎない（薄雲・破片雲・高太陽天頂角では MODIS が τ を出さない）。残り 39% を定数 0.55（τ ≈ 16 相当）で埋めており、これが全球平均の押し上げ要因の 1 位（寄与 0.14）だった。

改良: 通常 τ レイヤ → 部分雲 `MODIS_Terra_Cloud_Optical_Thickness_PCL` レイヤ（+4%）の順に採り、それでも無い雲画素は **同じ 10° 緯度帯の取得画素の雲分率重み平均 α_c** で埋める（`--zonal-band-deg`、全球平均 0.369）。定数 `cloud_albedo` は τ が全く無い時の最終フォールバックに格下げ。

### 3.2 スワース間隙が晴天扱いだった

MODIS Terra の軌道間隙（熱帯に斜めの帯、面積 6%）が NaN → 雲分率 0 → 晴天の海として暗い縞になっていた。改良: 行ごとに経度方向の周期線形補間で埋める（`fill_gaps_along_longitude`、`--no-fill-gaps` で従来動作）。

### 3.3 雪氷上の雲が地表より暗くなっていた

旧式 A = f·α_c + (1−f)·clear は雲の下の地表を無視するため、南極（α_s ≈ 0.85）で雲画素（α_c ≈ 0.4〜0.55）が地表より暗くなる不整合があった。改良: 曇天側を雲と地表の多重反射込み

A_cloudy = α_c + (1 − α_c)²·α_s / (1 − α_c·α_s)

にした（Lacis & Hansen 型、吸収なし）。海（α_s 0.06）では +0.02 程度。

### 3.4 統計の追加

CERES の惑星アルベドは「反射 / 入射」なので、比較には**日射量 × 面積**の重みが要る（面積平均は低日射の極域の高アルベドを過大評価する）。stats（sidecar JSON）に `global_mean_albedo_insolation_weighted`（日付の太陽赤緯から日平均 TOA 日射量を計算）、`cloud_tau_coverage`、`cloud_gap_filled_area`、`cloud_albedo_fill_global_mean` を追加した。

## 4. 最終マップ（2026-03-20、level 2）

| 指標 | 改良前 | 改良後 |
|---|---|---|
| 全球平均アルベド（面積重み） | 0.379 | **0.377** |
| 全球平均アルベド（日射量重み） | 0.356 | **0.343** |
| 雲分率（面積平均） | 0.657 | 0.708（間隙補間で増） |
| τ 取得率（雲分率重み） | 0.61 | 0.60（+PCL、分母も増） |
| 地表アルベド平均（BMNG 推定） | 0.159 | 0.159 |
| CERES 参照値（年平均） | 0.29〜0.30 | 0.29〜0.30 |

改良後も日射量重みで CERES より約 15% 高い。3.1 と 3.3 が相殺気味（3.1 で −0.04、3.3 と間隙補間で +0.03）で、残る上振れの要因は次の 2 つと考えられる。

- τ 未取得の雲は光学的に薄い側に偏るはずで、緯度帯平均（0.37）は依然過大。ここを 0.25 程度にすると CERES に乗るが、根拠のない調整になるので行っていない。
- 二流近似 τ(1−g)/(2+τ(1−g)) は垂直入射・保存散乱の式で、MODIS の τ 分布（雲分率重み平均 16、中央値 8）と組み合わせると雲アルベドがやや高めに出る。

したがって、このマップは**エネルギー収支の上限側**（雲を厚めに見積もる）の地球照光源として使う。下限側は BMNG テクスチャ地球（海 0.02、雲なし）で、真値はその間にある。

図: [reports/figures/albedo_2026-03-20_track.png](figures/albedo_2026-03-20_track.png)（マップ + Hubble 軌道の地上軌跡）、[reports/figures/albedo_2026-03-20_diag.png](figures/albedo_2026-03-20_diag.png)（雲分率 / τ 取得状況 / 最終マップ）。

## 5. Mitsuba 計測の独立検証

`presets/pv_hubble_orbit.yaml`（Hubble 軌道、エポック 2026-03-20T12:00 UTC）の反太陽セル（法線 = 反太陽方向、1 m²）について、Mitsuba の irradiancemeter 計測と、ECEF 格子（720×1440）でランバート地球の地球照を直接積分した値を比較した。積分側はマップを ECEF 経度緯度で参照し、GMST で ECI に回すので、経度向きと自転の規約も含めた検証になる。

| t [s] | 直下点 lat/lon | 太陽天頂角 | 一様 0.3 球: Mitsuba / 積分 | マップ: Mitsuba / 積分 |
|---|---|---|---|---|
| 0 | 0.0° / 62.0°E | 60° | 90.2 / 89.6 W/m² | 58.0 / 58.2 W/m² |
| 1900 | 24.5° / 176.9°E | 155°（食） | 0 / 0 | 0 / 0 |
| 3800 | −24.1° / 78.1°W | 67° | 64.6 / 59.7 W/m²※ | 57.1 / 56.4 W/m² |

※ t=3800 の一様球は Mitsuba 側の spp 16 スモークラン値。差はいずれも 1.3% 以内（一様球 t=3800 は 8%、低 spp のノイズ）。BMNG テクスチャ地球は同じ t=0 で 2.8 W/m²。

## 6. 本番ラン

`runs/20260911_210658_relative_relative`（90 フレーム、640×480、64 spp、PV 16384 spp）。CSV は [reports/figures/pv_hubble_albedo_gibs_2026-03-20.csv](figures/pv_hubble_albedo_gibs_2026-03-20.csv) に複写。

| パネル | 直達 平均 | 地球照 平均 | 地球照 最大 | 相互反射 平均 | 発電 平均 | 日照率 |
|---|---|---|---|---|---|---|
| wing_plus_x（2.5×7.1 m） | 862.0 | 4.8 | 21.4 | 0.6 | 4619 W | 0.63 |
| wing_minus_x | 862.0 | 4.5 | 26.2 | 4.5 | 4638 W | 0.63 |
| aft_cell（反太陽 1 m²） | 0.0 | 68.7 | 277.9 | 0.0 | 20.6 W | 0.63 |

単位は W/m²。反太陽セルの地球照は日照中平均 108.5 W/m²、最大はフレーム 76（t = 4813 s、地球の昼側が正面）。図: [reports/figures/pv_hubble_albedo_frame_0076.png](figures/pv_hubble_albedo_frame_0076.png)。

## 7. 変更ファイル

- `earth_albedo_map.py`: §2 の 3 修正、§3 の改良、MCD43 スケール、stats 追加、CLI に `--tau-pcl-layer` / `--no-fill-gaps` / `--zonal-band-deg`
- `tests/test_earth_albedo_map.py`: 格子規約（level 0 = 640×320）と多重反射項に合わせて期待値更新。6 件 pass
- `CLAUDE.md`: 2 層アルベドマップ節を実機で確認した規約に書き換え
- `reports/figures/`: 本レポートの図と CSV

## 8. 未検証・残課題

- `--surface-source gibs`（MCD43 白空アルベド）はレイヤ id と TMS の存在、値スケールまで確認したが、マップ生成の通しは未実施。
- τ 未取得雲のアルベドの根拠（薄雲側への偏り）は文献値で裏取りしていない。CERES 一致を狙うなら、帯平均に対する係数か、MODIS の τ 未取得画素の統計が要る。
- 二流近似は太陽天頂角依存を持たない。斜め入射で雲アルベドが上がる効果は未考慮。
- 検証は 1 日付（春分）のみ。至点付近の日射量重みや極夜での挙動は未確認。
