# mRendering 物理検証レポート（2026-09-08）

対象コミット: 1a9d0e9（main, working tree clean）。方法: 既存回帰テスト・監査ツールの実行 +
第一原理／独立実装（Meeus・Vallado・Montenbruck・CIE1931 等）との数値比較。
検証スクリプトは scratchpad/{mine,agentA,agentB,agentC,agentD}/ に保存。プロジェクトファイルは無変更。

## 0. 既存テスト
- tests/test_physics_regression.py: 10/10 OK
- tools/physics_audit.py: 全チェック PASS

## 1. 正しいと確認できたもの（抜粋・数値）
| 領域 | 検証 | 結果 |
|---|---|---|
| Kepler/oe2rv/rv2oe | 独立 Kepler 解・往復 | 位置 <1e-6 km、往復 2e-13 |
| dcm_i2rtn | R=r/|r|, N=r×v, T=N×R と比較 | 1e-15 |
| GMST (yoshimulib) | Vallado Ex3-5 / Meeus 12.b / IAU82 | 312.8098943° 再現、1e-6° |
| shadow() | Montenbruck 円錐影 独立実装 | 1e-10（本影/半影/日照分類一致） |
| 姿勢 DCM 規約 | body→ECI 列ベクトル、to_world=T·R·S、nadir +z·(−r̂)=1 | 3e-16 |
| Euler 方程式 | 成分形と差 0、Dzhanibekov 反転再現、L_I ドリフト 4e-15 | 積分誤差 ≤3e-6°/run |
| クォータニオン | ω_body=(0,0,1), dt=π/2 → 機体+x が慣性+y（右手） | OK |
| directional 照度 | アルベド1ランバート面 → L=E/π | 比 1.00000 |
| トーンマップ | ガンマ 1 回のみ（二重なら PNG 174、実測 110≈理論 111） | OK |
| 地球テクスチャ経度 | rotation=0 で +x=グリニッジ、東向き自転、昼/雲/夜光/地表ターゲット一致 | dlon=dlat=0 |
| absolute 地球姿勢 | 合成 lon/lat テクスチャをレイ交差: 直下点 32.03°E vs 期待 32.05° | ≤0.08°、crop ON/OFF・球メッシュ/解析球 4 通り同値 |
| HCW CSV 整合 | abs_chief/deputy → RTN 相対 = rel_state_hcw.csv | 0.00 m |
| absolute tumble/lvlh | t=0 姿勢 = rel_quat、ω=0 で ECI 固定、ω≠0 で独立積分と 0.33″ | OK |
| groundobs 測光 | 1 m 球 α=0/60/90°、GEO と 1000 km | 解析比 1.0007、逆二乗 厳密 |
| groundobs 幾何 | WGS-84 ECEF、ENU、屈折(Meeus 16.4)、追尾トレイル方向、TLE epoch | 一致 |
| 星投影パリティ | Mitsuba レンダ重心 vs project_to_grid | 0.04 px、北上・東左 |
| 材質 | 全 BSDF の半球反射率 ≤1 | OK |
| 目視 | relative_ykwn_absolute 4 フレーム: 東アフリカ昼/シベリア夕/食で全黒/パタゴニア氷原 | 地理・照明整合 |

## 2. 問題点

### 重大（機能停止・出力が誤り）
1. **render / preview / lightcurve / onboard が CLI・GUI から起動不能**（回帰、コミット 1a9d0e9）
   scene_earth.py:64 `def earth_surface_transform(...) -> mi.ScalarTransform4f` の戻り値注釈が import 時に評価され、
   satellite_orbit.py が scene_builder→scene_earth を mi_variant.init_variant（scene_common）より前に import するため
   `AttributeError: Cannot access 'ScalarTransform4f' before setting a variant`。テストは scene_common を先に import するので検出不能。
   対処: scene_earth.py 冒頭に `from __future__ import annotations`、または satellite_orbit.py で mi_variant を最初に import。
2. **星空 envmap（assets/starfield.exr）の投影規約が Mitsuba と不一致で鏡像**
   generate_starfield: u=RA/2π, v=(0.5−Dec/π)（極=±z）。Mitsuba envmap 実測: u=atan2(x,−z)/2π, v=acos(y)/π（極=±y、u は極まわり左手回り）。
   ECI→envmap-local の写像は det=−1 → to_world の回転だけでは直らない。実測: シリウス方向にカメラを向けても 120° 離れた所に出る。
   render 系（scene_builder）・relative（rotate_env_dict）両方に影響。「ECI 固定」の回転自体は正しい。
   対処: 生成時に u=1−RA/2π かつ極を y に置く（or 画像左右反転 + 固定回転 Rx(−90°) を to_world に合成）。
3. **lightcurve verb の観測者が地球自転に追従しない**（satellite_orbit.py:523、ループ外で 1 回計算）
   1/4 周で 529 km、1 周で 2104 km ずれる。range/位相角/遮蔽判定すべてに影響。resolve_camera 側は自転させている。
4. **lightcurve 既定値では物体が視野をはみ出す**：satellite_scale 0.002（地球半径単位 → 本体 38 km 級、iss.yaml の ISS は 1437 km、頂点 1.2% が地表下）、light_curve_fov 2° → 全距離でクリップ、flux が距離・位相角の関数として無効。

### 要検討（精度・物理スケール）
5. **座標系混在: 太陽・恒星 = J2000、観測者(GMST)・TLE(TEME) = of-date**
   yoshimulib `sun_lon_lat_r` は docstring「equinox of date」だが、of-date 係数（Meeus 付録III）に precession(J2000→jd) を重ね、実際は J2000 黄経を返す
   （1992-10-13: raw 199.90737=Meeus 25.b 真値、出力 200.00821；2026 で差 0.367°、2050 で 0.70°）。
   → groundobs/relative absolute の太陽は J2000 系、恒星（Hipparcos ICRS）もそのまま。影響: 太陽位相角 0.37°（測光 <0.01 mag、食時刻 ~6 s）、
   **星野が 2026 年で約 22′ ずれる**（hubble FOV 25.6″、geo_point 64″ では視野内の星が別物、survey 68′ でも 1/3 幅）。
6. **groundobs 光電子数が約 8.6 倍過大**：ボロメトリック S0=1361 W/m² 由来の照度を 550 nm 光子エネルギーで割る（V バンド帯域内は ≈160 W/m²）。
   m=13, D=0.3 m, 1 s: 18,952 e⁻ vs 教科書 2,209 e⁻。限界等級が 1.2〜2.3 mag 甘い。等級 CSV は無影響。
7. **黒体色 RGB がガンマ空間**：Tanner Helland 式（sRGB 8bit 表示値）を線形照度に乗算。5778 K 線形 sRGB は (1,0.88,0.82)、コードは (1,0.951,0.904)（G+8%, B+10%）。最大成分正規化（輝度 0.958）。
8. **render 系に絶対エポックが無い**：太陽は円運動（t=0 で春分方向）、自転角は t=0 でグリニッジ=+x。暗黙のエポック「春分正午」が未文書。任意日付で 80° 級、春分に合わせても 9 月 4°。
   sun_angle モードは赤緯 +16.7° 固定（docstring は「黄道面に対する角度」）。
9. **食モデルの不統一**：lightcurve は本影のみ（半影中も全照度、GEO で半影 ~2 分）、render 系は幾何影（円柱 6378 km、真本影 LEO 6347/GEO 6184 km）、relative absolute は ν 乗算（正しい）。
10. **relative `--earth-direction` 既定 [0,0,−1] と docstring「−N=地心」が RTN 規約（地心=−R=[−1,0,0]）と矛盾**。既定だと北極が直下点。プリセットは全て上書き済み。
11. **akatsuki.yaml の慣性 Ix=4>Iy=2>Iz=1 が形状と逆**（obj 表面慣性 x:y:z=41:180:171、パドル軸 x が最小主軸）。akatsuki.yaml `view_mode: chase` は rotation パーサに無く黙って無視。
12. **材質**：white_paint 実効アルベド 0.53（AZ-93 ≈0.85）、ROUGHNESS rough=0.8 の Al で 0.59（マイクロファセット単散乱損失）。
13. **任意スケール（意図的だが定量性なし）**：太陽強度 5.0（1361 未使用）、環境光/星空が実際の 10⁴〜10⁶ 倍（影側輝度 +4〜9%）、rotation の座標軸 area emitter が衛星を +2〜4% 照らす（OFF 無し）、Mitsuba 大気シェル（191 km 一様・等方位相・τ 実際の 1/200）、夜間光輝度が昼側の 2 倍。
14. **optical_atmosphere.py（未使用ヘルパ）**：Rayleigh 係数が 680 nm 値、Mie 1/10、位相関数正規化不整合（4π vs 1）、compute_earth_albedo 1.49 倍過大。

### 軽微
- 恒星日 23.934 h（真 86164.09 s、−1.7 s/day）、黄道傾斜 23.5/23.44/23.4393 が混在、年 365.25 d、J2 1.08263e-3、球体地球観測者（WGS-84 と 21 km 差）。
- `--start-overhead` の「仰角誤差 <1°」は LEO で 87.4°（M 刻み 0.5°）。TLE epoch 自動採用が「既定文字列一致」判定。
- camera_up ∥ 視線の退避なし（relative manual、rotation は up 固定で ±y 軸上から NaN）。
- 純 2.2 乗ガンマ vs sRGB 区分曲線（暗部 PNG 23 vs 16）。
