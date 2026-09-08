# 物理計算の修正結果 第 2 次（2026-09-08、検証 #2〜#8）

[第 2 次検証](physics_verification_2026-09-08.md)で見つかった 8 件をすべて修正した。
回帰テスト `tests/test_physics_fixes_b.py`（17 件）と `tests/test_cli_import_order.py`（3 件、サブプロセスで
CLI 起動経路を検証）を追加。全テスト 46/46、`tools/physics_audit.py` 32/32。

| # | 修正 | 検証 |
|---|---|---|
| 1 import 順回帰 | `scene_earth.py` に `from __future__ import annotations`。戻り値注釈 `-> mi.ScalarTransform4f` がバリアント未設定の import 時に評価されていた（GUI のジョブ起動も同経路で全滅していた） | 新規インタプリタで satellite_orbit / 各 verbs.* の import と `mrender.py <verb> --help` が通る。preview/render/lightcurve/onboard を CLI から実行して完走 |

| # | 修正 | 検証 |
|---|---|---|
| 2 星空 envmap 鏡像 | `tools/generate_starfield.py` を星図規約 u = 1 − RA/2π に変更、`optical_lighting.STARFIELD_LOCAL_TO_ECI`（det +1）を scene_builder / relative_motion（absolute は A_i2rtn·R）の to_world に合成。`assets/starfield.exr` 再生成（8192×4096）。星色も線形黒体色に | Mitsuba envmap の (u,v) 参照と生成画素が 1 px 以内で一致（7 方向）。新 EXR でシリウス/ベガ/北極星/カノープス方向へレンダ → 最輝画素が中心から ≤0.044°（旧 EXR はシリウスが 120° 離れていた） |
| 3 lightcurve 観測者非回転 | `satellite_orbit.observer_position_eci_km()` をフレーム毎に呼び、地表ターゲットと同じ `compute_earth_rotation_deg` で回す | 赤道・経度 0 の観測者が 1/4 自転で +y（1e-9） |
| 4 lightcurve 視野クリップ | `light_curve_fov` 既定を None（自動）に。`estimate_bounding_radius`（手続き形状/メッシュ頂点）× 1.5 から画角を決定。数値指定は従来どおり | 既定スケールで自動 6.95°（従来 2° で全距離クリップ）、flux が非ゼロに |
| 5 J2000 / of-date 混在 | `orbit_mechanics.sun_position_eci_km(jd, frame='date')`（VSOP87 J2000 → `precession_j2000_to_date` で MOD）、groundobs `stars_in_field` で Hipparcos を同じ P で歳差補正。groundobs / relative absolute は再エクスポート経由で共用。yoshimulib `sun_lon_lat_r` の docstring を実態（J2000）に修正（実装は `sun()` と整合しているので不変） | of-date 黄経が Meeus 低精度式と 0.02° 以内（1992/2026/2050）、直接計算の of-date 太陽と 0.0″。J2000 との差 0.3668°（2026） |
| 6 光電子 ×8.6 | `photons_per_wm2(S0)` = F_ph(V=0)·10^(0.4·26.74)/S0（F_ph(V=0)=8.84e9 ph/s/m²、Bessell）。物体・空背景で共用 | m_V=13, D=0.3 m, 1 s, QE 0.8, τ 0.7 → 2,209 e⁻（旧 18,952） |
| 7 黒体色ガンマ | `blackbody_to_rgb` を Planck × CIE 1931（Wyman 2013）→ 線形 sRGB（最大成分 1）に置換。generate_starfield も共用 | 5778 K = (1, 0.880, 0.823)（旧 (1, 0.951, 0.904)）、6504 K ≈ (1, 0.945, 0.993) |
| 8 render 系エポック | `epoch_utc`（`_ARG_DEFAULTS`、`AnimationConfig.epoch_jd`）。指定時: 太陽 = VSOP87 of-date、自転角 = GMST。未指定: 従来の簡易モデル不変。`sun_angle` を黄経（黄道傾斜 23.44°、以前は赤緯 +16.7° 固定）に。GUI に `epoch_utc` 欄追加 | t=0 太陽 = `sun_direction_at_jd(jd0)`、1 h で GMST +15.041°、epoch 無しは [1,0,0]/0° のまま |

実行スモーク: `groundobs_geo_survey`（2 フレーム、恒星あり）、`relative_ykwn_absolute`（2 フレーム、新星空）、
`render_light_curve`（epoch 指定・自動画角、Python から直接呼び出し）が完走。

注意: 星空 EXR は Git 管理外なので、他環境では `tools/generate_starfield.py --width 8192 --milky-way --output assets/starfield.exr`
で再生成すること（旧 EXR を新 to_world で使うと鏡像のまま 90° 回る）。
