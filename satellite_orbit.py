#!/usr/bin/env python3
"""
地球周回物体の絶対軌道・姿勢運動を Mitsuba 3 で可視化する。

このモジュールは ``mrender`` の ``render`` / ``lightcurve`` / ``preview`` 動詞の
コア実装である。``verbs/render.py``・``verbs/lightcurve.py`` などの薄い verb 層
から呼び出され、CLI シム ``main()`` も後方互換のため verb 群へディスパッチする。

全体データフロー:
    1. config_loader.build_parser / load_yaml_config → argparse.Namespace
    2. config_loader.build_render_config → RenderConfig + List[ObjectSpec]
    3. compute_frame_time → 各フレームの時刻 t [s]
    4. resolve_sun_direction → ECI 上の太陽方向ベクトル
    5. compute_object_state (内部で yoshimulib の oe2rv / rv2oe / dcm_i2rtn を使用)
       → 位置 [km]・速度 [km/s]・姿勢行列 (Body→ECI)
    6. build_scene_objects → scene_objects (Mitsuba 用 dict 断片)
    7. resolve_camera + build_earth_textures → カメラと地球テクスチャ
    8. scene_builder.create_scene → Mitsuba シーン辞書 → mi.render → PNG

使用方法:
    source venv/bin/activate
    python satellite_orbit.py --config presets/iss_basic.yaml
"""

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import drjit as dr
import mitsuba as mi
import numpy as np

from orbit_mechanics import (
    EARTH_RADIUS_KM,
    SCALE_FACTOR,
    compute_attitude_matrix,
    compute_observer_position_km,
    compute_orbital_position,
    compute_sun_position,
    compute_target_position_km,
    gmst_rad,
    is_earth_occluded,
    mean_obliquity_rad,
    sun_direction_at_jd,
)
from scene_builder import (
    apply_tonemap,
    compute_camera_up,
    compute_image_flux,
    create_scene,
)
from scene_earth import (
    resolve_cloud_opacity_texture,
    resolve_night_emission_texture,
    rotate_vector_z,
)
from asset_cache import preload_bitmaps, share_bsdf_textures
from scene_objects import create_trajectory_trail, create_satellite, load_external_model

from yoshimulib.orbit.transforms import shadow as earth_shadow_function
from yoshimulib.orbit.orbital_elements import AU_KM as SUN_DISTANCE_KM

from render_config import (
    DEFAULT_ORBIT_SETTINGS,
    RenderConfig,
    ObjectSpec,
    ObjectState,
)
from config_loader import (
    build_parser,
    build_render_config,
    load_yaml_config,
    resolve_earth_day_texture,
    resolve_object_specs,
    validate_model_paths,
)

# Mitsuba のバリアント設定。既定は LLVM、MRENDER_VARIANT 環境変数で切替
# （例: MRENDER_VARIANT=cuda_ad_rgb,llvm_ad_rgb）。詳細は mi_variant.py。
from mi_variant import init_variant
init_variant(mi)

# 太陽の半径 [km]（食判定で太陽視半径を求めるのに使う。yoshimulib には定数が無い）。
SUN_RADIUS_KM = 695700.0


def is_in_earth_shadow(position_km: np.ndarray, sun_direction: np.ndarray) -> bool:
    """物体が地球の本影（umbra）に入っているかを判定する。

    yoshimulib の ``shadow()``（Montenbruck & Gill p.81 の円錐影モデル）を使い、
    太陽視半径と地球視半径の重なりから照射率 ν∈[0,1] を求める。
    ν = 1 が全照、0 < ν < 1 が半影、ν = 0 が本影。

    Args:
        position_km: 物体の ECI 位置 [km]。
        sun_direction: 地心→太陽の単位ベクトル（``resolve_sun_direction`` の戻り値）。

    Returns:
        本影内なら True。半影・全照は False（LEO の半影通過は数秒で、
        ライトカーブのサンプル間隔に対して無視できるため）。
    """
    # shadow() は太陽「位置」を要求するので、単位方向を 1 au 伸ばして与える。
    sun_position_km = np.asarray(sun_direction, dtype=float) * SUN_DISTANCE_KM
    nu = earth_shadow_function(
        np.asarray(position_km, dtype=float),
        sun_position_km,
        SUN_RADIUS_KM,
        EARTH_RADIUS_KM,
    )
    return bool(float(np.atleast_1d(nu)[0]) <= 0.0)


def prefix_scene_objects(objects: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """シーン辞書のキー衝突を避けるため、物体名プレフィックスを付与する。

    Mitsuba のシーン辞書は同名キーを許さないので、複数物体を同居させるとき
    ``{prefix}_{key}`` 形式に書き換えて衝突を回避する。
    """
    return {f'{prefix}_{key}': value for key, value in objects.items()}


def compute_frame_time(frame: int, total_frames: int, config: RenderConfig, period_s: float) -> float:
    """フレーム番号を、軌道計算に使う時刻 [s] へ変換する。

    2 通りの時間軸モードをサポートする:
        - duration_sec 指定: 実時間 (fps 指定 or total_frames から推定) を使用
        - 未指定:           軌道周期 period_s に対するフレーム比で時間を割り当て

    どちらも ``orbit_speed`` 倍率と ``start_time`` オフセットを適用する。
    戻り値は秒（軌道伝播・姿勢計算に渡す絶対時刻として扱う）。
    """
    start_t = config.animation.start_time
    if config.animation.duration_sec is not None:
        fps = config.animation.fps
        if fps is None:
            fps = max(total_frames / config.animation.duration_sec, 1e-6)
        return start_t + (frame / fps) * config.animation.orbit_speed
    # 周期ベース: total_frames で 1 周回するように時間スケールを取る。
    return start_t + (frame / total_frames) * period_s * config.animation.orbit_speed


def default_time_span_s(primary: ObjectSpec) -> float:
    """duration_sec 未指定時に使う既定の時間スパン [s] を返す。

    CSV エフェメリスがある場合は CSV の収録時間を優先する。そうしないと、
    CSV 姿勢を読み込んでいても軌道周期ベースの時刻でサンプリングされ、
    姿勢アニメーションが意図しない速度・位相に見える。
    """
    if primary.ephemeris is not None and primary.ephemeris.duration > 0:
        return primary.ephemeris.duration
    return primary.orbital_elements.orbital_period


def compute_object_state(spec: ObjectSpec, time_s: float, sun_direction: np.ndarray) -> ObjectState:
    """指定時刻における 1 物体の位置・速度・姿勢をまとめて計算する。

    優先順位は ephemeris > 数値伝播 > ケプラー解析解。

    - ``spec.ephemeris``: CSV 由来のエフェメリス（``CsvEphemeris``）。位置/速度を
      時刻補間で取得し、CSV にクォータニオン列があれば姿勢行列も得る。
    - ``spec.propagator``: ``solve_ivp`` ベースの数値伝播器（J2 や drag を含む）。
    - 上記いずれも無い場合: yoshimulib の ``oe2rv`` を内部で使う解析解
      （``compute_orbital_position``）でケプラー軌道を伝播する。

    座標系: 位置 [km]・速度 [km/s] はいずれも ECI。姿勢行列は Body→ECI の 3x3。
    """
    if spec.ephemeris is not None:
        # CSV エフェメリス: time_s に対し補間（範囲外は端点クランプ＋警告）。
        position_km, velocity_km = spec.ephemeris.state_at(time_s)
        attitude_matrix = spec.ephemeris.attitude_at(time_s)
        if attitude_matrix is None:
            attitude_matrix = compute_attitude_matrix(
                position_km, velocity_km,
                mode=spec.attitude_mode, sun_direction=sun_direction,
            )
    elif spec.propagator is not None:
        # 数値伝播（摂動付き）。姿勢は別途 attitude_mode に従って構成する。
        position_km, velocity_km = spec.propagator.state_at(time_s)
        attitude_matrix = compute_attitude_matrix(
            position_km, velocity_km,
            mode=spec.attitude_mode, sun_direction=sun_direction,
        )
    else:
        # 解析的ケプラー伝播（yoshimulib.orbit.orbital_elements.oe2rv 経由）。
        position_km, velocity_km = compute_orbital_position(spec.orbital_elements, time_s)
        attitude_matrix = compute_attitude_matrix(
            position_km, velocity_km,
            mode=spec.attitude_mode, sun_direction=sun_direction,
        )
    return ObjectState(
        spec=spec,
        time_s=time_s,
        position_km=position_km,
        velocity_km=velocity_km,
        attitude_matrix=attitude_matrix,
    )


def frame_jd(time_s: float, config: RenderConfig) -> Optional[float]:
    """フレーム時刻のユリウス日。epoch_utc 未指定なら None（簡易モデル経路）。"""
    epoch_jd = getattr(config.animation, 'epoch_jd', None)
    if epoch_jd is None:
        return None
    return float(epoch_jd) + float(time_s) / 86400.0


def ecliptic_longitude_to_direction(lon_deg: float, jd: Optional[float] = None) -> np.ndarray:
    """黄経 λ [deg]（黄緯 0）→ ECI 単位ベクトル。黄道傾斜は of-date（既定 J2000 値）。"""
    lam = np.radians(lon_deg)
    eps = mean_obliquity_rad(jd if jd is not None else 2451545.0)
    return np.array([np.cos(lam), np.sin(lam) * np.cos(eps), np.sin(lam) * np.sin(eps)])


def resolve_sun_direction(time_s: float, config: RenderConfig) -> np.ndarray:
    """指定時刻の太陽方向（ECI 単位ベクトル）を決める。

    優先順位:
        1. ``sun_rotate`` が真: 演出用に時刻と共に回転する太陽方向で上書き
           （黄経が周期 ``sun_rotate_period_s``、倍率 ``sun_rotate_speed`` で進む）
        2. ``sun_angle`` 指定: 太陽の黄経 [deg]（黄緯 0、黄道傾斜 23.44°）から固定方向
           （0 = 春分点方向 +x、90 = 夏至方向で赤緯 +23.44°）
        3. ``epoch_utc`` 指定: VSOP87 による実時刻の太陽（平均分点 of date）
        4. それ以外: ``compute_sun_position(time_s)`` の簡易円運動
           （t=0 で太陽 = +x = 春分点方向という暗黙のエポック）

    戻り値は地球中心から太陽へ向かう ECI 単位ベクトル。
    """
    jd = frame_jd(time_s, config)
    if config.lighting.sun_angle is not None:
        sun_direction = ecliptic_longitude_to_direction(float(config.lighting.sun_angle), jd)
    elif jd is not None:
        sun_direction = sun_direction_at_jd(jd)
    else:
        sun_direction = compute_sun_position(time_s)

    if config.lighting.sun_rotate:
        # 演出モード: 太陽の黄経を時刻 t に応じて回転させる。
        period_s = max(config.lighting.sun_rotate_period_s, 1e-6)
        angle = (time_s / period_s) * 360.0 * config.lighting.sun_rotate_speed
        sun_direction = ecliptic_longitude_to_direction(angle, jd)
    return sun_direction / np.linalg.norm(sun_direction)


def build_scene_objects(states: List[ObjectState], config: RenderConfig,
                        trail_histories: Optional[Dict[str, List[np.ndarray]]] = None) -> Dict[str, Any]:
    """各物体の状態から Mitsuba 用のシーン辞書断片を組み立てる。

    各物体について:
        - ECI 位置 [km] を ``SCALE_FACTOR`` でシーン座標へ変換
        - ``model_path`` 指定があれば外部 OBJ/PLY/GLB をロード
          （材質は ``model_material`` でオーバーライド可能）
        - 無ければ ``create_satellite`` で手続き的衛星モデルを生成
        - ``show_orbit`` が真かつ trail 履歴が 2 点以上溜まっていれば軌跡線を追加

    ``view_mode == 'satellite'`` のときは、自分自身がカメラになる物体だけ
    シーンから除外する（画面に映らないようにする）。
    ``earth_only`` モードでは物体は一切配置せず空辞書を返す。
    """
    scene_objects: Dict[str, Any] = {}
    if config.earth.earth_only:
        return scene_objects

    # 衛星搭載カメラ視点では、撮像元の物体自身は画面に出さない。
    hide_camera_object = config.camera.view_mode == 'satellite'
    camera_name = config.camera.view_camera_name
    for index, state in enumerate(states):
        if hide_camera_object:
            # 名前指定があれば一致するもの、なければ states[0]（primary）を除外。
            if camera_name is not None:
                if state.spec.name == camera_name:
                    continue
            elif index == 0:
                continue
        # ECI [km] → シーン単位（SCALE_FACTOR は km→シーン単位の換算係数）。
        position_render = state.position_km * SCALE_FACTOR
        prefix = state.spec.name.replace(' ', '_')
        if state.spec.model_path:
            # 外部 3D モデル経路: パーツ別 BSDF/材質オーバーライドに対応。
            objects = load_external_model(
                state.spec.model_path,
                position_render,
                state.attitude_matrix,
                scale=state.spec.scale,
                material_name=state.spec.model_material,
                keep_materials=state.spec.model_keep_materials,
                prefix=prefix,
            )
        else:
            # プロシージャル衛星（本体・ソーラーパネル・アンテナ）。
            objects = prefix_scene_objects(
                create_satellite(
                    position_render,
                    state.attitude_matrix,
                    scale=state.spec.scale,
                    use_advanced_materials=config.lighting.use_advanced_optics,
                ),
                prefix,
            )
        scene_objects.update(objects)

        if config.lighting.show_orbit and trail_histories is not None:
            # 軌跡: 過去フレームの位置（ECI [km]）から線分メッシュを生成。
            trail = trail_histories.get(prefix, [])
            if len(trail) >= 2:
                orbit = prefix_scene_objects(
                    create_trajectory_trail(
                        trail,
                        line_radius=config.lighting.orbit_line_radius,
                        color=config.lighting.orbit_line_color,
                        glow=config.lighting.orbit_line_glow,
                    ),
                    f'{prefix}_trail',
                )
                scene_objects.update(orbit)

    return scene_objects


def resolve_camera(states: List[ObjectState], config: RenderConfig) -> Tuple[Sequence[float], Sequence[float], Sequence[float], float]:
    """物体状態群と view_mode からカメラ姿勢を決定する。

    `view_camera_name` が指定されていればその物体をカメラ位置・姿勢として採用、
    未指定なら states[0]（primary）を使う。`view_target_name` で別物体を注視可能。

    返り値は ``(camera_position, camera_target, camera_up, fov_deg)`` で、
    位置はシーン単位（km × SCALE_FACTOR）、fov は度。

    view_mode の意味:
        - ``satellite``: 物体に固定された搭載カメラ視点（広角 90°）
        - ``chase``:     物体後方から物体本体を注視する三人称視点（65°）
        - ``earth``:     地球を真横から見る固定外部視点（45°）
        - その他:         物体を含む俯瞰の慣性系視点 inertial（95°、または inertial_fixed）
    最後に ``camera.overrides`` の値があれば一括で上書きできる。
    """
    by_name: Dict[str, ObjectState] = {state.spec.name: state for state in states}
    camera_obj: ObjectState
    if config.camera.view_camera_name is not None:
        if config.camera.view_camera_name not in by_name:
            raise ValueError(
                f"view_camera_name '{config.camera.view_camera_name}' に該当する物体がありません。"
                f" 候補: {list(by_name.keys())}"
            )
        camera_obj = by_name[config.camera.view_camera_name]
    else:
        camera_obj = states[0]

    # カメラ基準物体の位置・速度をシーン単位に変換しておく。
    position_render = camera_obj.position_km * SCALE_FACTOR
    velocity_render = camera_obj.velocity_km * SCALE_FACTOR

    if config.camera.view_mode == 'satellite':
        # 搭載カメラ: 物体位置にカメラを置き、注視先を別途決める。
        camera_position = position_render
        target_state: Optional[ObjectState] = None
        if config.camera.view_target_name is not None:
            # 他物体を注視（ランデブ・近接運用の可視化）。
            if config.camera.view_target_name not in by_name:
                raise ValueError(
                    f"view_target_name '{config.camera.view_target_name}' に該当する物体がありません。"
                    f" 候補: {list(by_name.keys())}"
                )
            target_state = by_name[config.camera.view_target_name]
            camera_target = (target_state.position_km * SCALE_FACTOR).tolist()
        elif config.camera.target_lat is not None and config.camera.target_lon is not None:
            # 地表ターゲット: 緯度経度から ECI に変換して注視。
            target_km = compute_target_position_km(
                config.camera.target_lat,
                config.camera.target_lon,
                config.camera.target_alt_km,
            )
            target_km = rotate_vector_z(target_km, compute_earth_rotation_deg(camera_obj.time_s, config))
            camera_target = (target_km * SCALE_FACTOR).tolist()
        else:
            # デフォルトは地球中心（NADIR 撮像）。
            camera_target = [0, 0, 0]

        # up の決定: 視線と直交になるよう調整。
        # まず候補 up = camera 物体の Y 軸（nadir 姿勢なら along-track）。
        # 視線とほぼ平行なら世界 Z ([0,0,1]) にフォールバック。
        # 最後に視線方向と直交化（Gram-Schmidt）してロール暴れを防ぐ。
        view_dir = np.asarray(camera_target, dtype=float) - np.asarray(camera_position, dtype=float)
        view_norm = np.linalg.norm(view_dir)
        if view_norm > 1e-12:
            view_dir = view_dir / view_norm
            up_candidate = camera_obj.attitude_matrix @ np.array([0.0, 1.0, 0.0])
            if abs(np.dot(view_dir, up_candidate)) > 0.99:
                up_candidate = np.array([0.0, 0.0, 1.0])
            up_orth = up_candidate - np.dot(up_candidate, view_dir) * view_dir
            n = np.linalg.norm(up_orth)
            camera_up = (up_orth / n) if n > 1e-9 else np.array([0.0, 0.0, 1.0])
        else:
            camera_up = camera_obj.attitude_matrix @ np.array([0.0, 1.0, 0.0])
        fov = 90.0
    elif config.camera.view_mode == 'chase':
        # 追従視点: 物体の後方（-velocity 方向）かつ少し外側（+radial）にカメラ。
        satellite_distance = np.linalg.norm(position_render)
        radial_out = position_render / satellite_distance  # 地心→物体の動径単位ベクトル
        velocity_normalized = velocity_render / np.linalg.norm(velocity_render)
        camera_position = (
            position_render
            - velocity_normalized * config.camera.chase_back  # 後方へオフセット
            + radial_out * config.camera.chase_out            # 外側へオフセット
        )
        camera_target = position_render        # chase は物体本体を画面内に保つ
        camera_up = radial_out                  # up は地心方向の逆（天頂）
        fov = 65.0
    elif config.camera.view_mode == 'earth':
        # 地球を真横から見る固定カメラ（演出・サムネ用）。
        camera_position = [3.0, 0.0, 1.2]
        camera_target = [0.0, 0.0, 0.0]
        camera_up = [0.0, 0.0, 1.0]
        fov = 45.0
    else:
        # inertial（慣性系俯瞰）: inertial_fixed なら原点固定、そうでなければ物体に追従。
        if config.camera.inertial_fixed:
            distance = config.camera.inertial_distance
            camera_position = [distance, 0.0, distance * 0.35]
            camera_target = [0.0, 0.0, 0.0]
            camera_up = [0.0, 0.0, 1.0]
        else:
            camera_position = position_render * 2.5
            camera_target = position_render * 0.3
            camera_up = [0.0, 0.0, 1.0]
        fov = 95.0

    # 最後にユーザ指定オーバーライドを適用（CLI/YAML の camera.* origin/target/fov/up）。
    if config.camera.overrides.origin is not None:
        camera_position = config.camera.overrides.origin
    if config.camera.overrides.target is not None:
        camera_target = config.camera.overrides.target
    if config.camera.overrides.fov is not None:
        fov = config.camera.overrides.fov
    if config.camera.overrides.up is not None:
        camera_up = config.camera.overrides.up

    return camera_position, camera_target, camera_up, fov


def compute_earth_rotation_deg(time_s: float, config: RenderConfig) -> float:
    """地表ターゲット・観測者・バッチ・ライブ描画が共用する地球自転角 [deg]。

    自転角 0 でグリニッジ子午線が ECI +x（テクスチャ経度 0°）。``epoch_utc`` 指定時は
    GMST（IAU 1982、of-date 分点）そのもので、``earth_rotation_period_hours`` /
    ``earth_rotation_speed`` は使わない。未指定なら t=0 で 0° の簡易モデル。
    """
    if not config.earth.earth_rotation:
        return 0.0
    jd = frame_jd(time_s, config)
    if jd is not None:
        return float(np.degrees(gmst_rad(jd)))
    seconds_per_day = config.earth.earth_rotation_period_hours * 3600.0
    if not np.isfinite(seconds_per_day) or seconds_per_day <= 0:
        raise ValueError('earth_rotation_period_hours は有限の正数である必要があります')
    return (time_s / seconds_per_day) * 360.0 * config.earth.earth_rotation_speed


def build_earth_textures(
    output_dir: Path,
    frame: int,
    time_s: float,
    sun_direction: np.ndarray,
    config: RenderConfig,
) -> Tuple[float, Optional[Path], Optional[Path]]:
    """地球の自転角と、夜間光・雲のフレーム別テクスチャを準備する。

    返り値: ``(earth_rotation_deg, night_emission_path, cloud_opacity_path)``

    - ``earth_rotation_deg``: 地球メッシュに掛ける Z 軸回りの自転角 [deg]
    - 夜間光: 太陽方向を地球本体座標系に逆回転してから、昼夜マスクを生成し、
      夜光テクスチャと合成して PNG に出力する。太陽方向・自転角が前フレームと
      同じなら生成済み PNG を使い回す（scene_earth 側でメモ化）
    - 雲: cloud_opacity が 1.0 でなければ EXR を生成（内容はフレーム非依存なので
      ラン中 1 度だけ）、1.0 ならテクスチャを直接使う
    """
    earth_rotation_deg = compute_earth_rotation_deg(time_s, config)

    night_emission_texture: Optional[Path] = None
    cloud_opacity_texture: Optional[Path] = None

    if config.earth.use_night_lights and config.earth.night_texture and Path(config.earth.night_texture).exists():
        # マスク解像度（最低でも 16x8）。経度方向を 2 倍取って equirectangular に合わせる。
        mask_w = max(int(config.earth.night_mask_res), 16)
        mask_h = max(int(mask_w // 2), 8)
        # 太陽方向を地球の自転と逆向きに回し、本体固定座標系での太陽方向を得る。
        sun_dir_body = rotate_vector_z(sun_direction, -earth_rotation_deg)
        night_dir = output_dir / 'night_textures'
        night_dir.mkdir(parents=True, exist_ok=True)
        # 夜側だけ発光し昼側はマスクで暗くしたテクスチャ。太陽方向・自転角が
        # 前フレームと同じなら resolve_* 側で前フレームの PNG を再利用する
        # （太陽固定＋自転オフのランでは 1 枚しか生成されない）。
        night_emission_texture = resolve_night_emission_texture(
            config.earth.night_texture,
            sun_dir_body,
            mask_w,
            mask_h,
            config.earth.night_terminator_softness,
            night_dir / f'night_emission_{frame:04d}.png',
        )

    if config.earth.use_clouds and config.earth.cloud_texture and Path(config.earth.cloud_texture).exists():
        if abs(config.earth.cloud_opacity - 1.0) > 1e-6:
            # opacity が 1.0 でない場合のみ EXR を生成（内容はフレームに依存しない
            # ので、resolve_* 側のメモ化によりラン中 1 度だけ書き出される）。
            cloud_dir = output_dir / 'cloud_textures'
            cloud_dir.mkdir(parents=True, exist_ok=True)
            cloud_opacity_texture = resolve_cloud_opacity_texture(
                config.earth.cloud_texture,
                config.earth.cloud_opacity,
                cloud_dir / 'cloud_opacity.exr',
            )
        else:
            cloud_opacity_texture = Path(config.earth.cloud_texture)

    return earth_rotation_deg, night_emission_texture, cloud_opacity_texture


def observer_position_eci_km(time_s: float, config: RenderConfig,
                             observer_lat: float, observer_lon: float,
                             observer_alt_km: float) -> np.ndarray:
    """時刻 t の地上観測者 ECI 位置 [km]（地表ターゲットと同じ自転角で回す）。"""
    fixed = compute_observer_position_km(observer_lat, observer_lon, observer_alt_km)
    return rotate_vector_z(fixed, compute_earth_rotation_deg(time_s, config))


_MESH_RADIUS_CACHE: Dict[Tuple[str, float], float] = {}


def _mesh_local_radius(path: str) -> float:
    """OBJ/PLY(ASCII) 頂点の原点からの最大距離（モデルローカル単位）。失敗時は 1.0。"""
    try:
        key = (str(path), Path(path).stat().st_mtime)
    except OSError:
        return 1.0
    if key in _MESH_RADIUS_CACHE:
        return _MESH_RADIUS_CACHE[key]
    r_max_sq = 0.0
    try:
        with open(path, 'r', errors='ignore') as handle:
            for line in handle:
                if line.startswith('v '):
                    parts = line.split()
                    if len(parts) >= 4:
                        x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                        r_max_sq = max(r_max_sq, x * x + y * y + z * z)
    except (OSError, ValueError):
        return 1.0
    radius = float(np.sqrt(r_max_sq)) if r_max_sq > 0 else 1.0
    _MESH_RADIUS_CACHE[key] = radius
    return radius


def estimate_bounding_radius(objects: Dict[str, Any], center: np.ndarray) -> float:
    """シーン辞書断片（to_world 付き shape 群）の ``center`` まわりバウンディング半径。

    各 shape について |平行移動 − center| + ローカル半径 × 最大スケール。手続き形状
    （cube/sphere/cylinder）はローカル半径 √3、メッシュは頂点から求める。
    """
    radius = 0.0
    for obj in objects.values():
        if not isinstance(obj, dict) or 'to_world' not in obj:
            continue
        matrix = np.asarray(obj['to_world'].matrix, dtype=float).reshape(4, 4)
        translation = matrix[:3, 3]
        scale = float(np.max(np.linalg.norm(matrix[:3, :3], axis=0)))
        if obj.get('type') in ('obj', 'ply') and obj.get('filename'):
            local = _mesh_local_radius(str(obj['filename']))
        else:
            local = float(np.sqrt(3.0))
        radius = max(radius, float(np.linalg.norm(translation - center)) + local * scale)
    return radius


def resolve_light_curve_fov(fov: Optional[float], objects: Dict[str, Any],
                            target_scene: np.ndarray, range_scene: float,
                            margin: float = 1.5) -> float:
    """ライトカーブ用カメラの画角 [deg]。明示指定があればそれ、None なら物体の
    見かけサイズ（バウンディング半径 × margin）から自動で決めてクリップを防ぐ。"""
    if fov is not None:
        return float(fov)
    radius = estimate_bounding_radius(objects, target_scene)
    if radius <= 0 or range_scene <= 0:
        return 2.0
    ratio = min(margin * radius / range_scene, 0.99)
    return float(np.clip(np.degrees(2.0 * np.arctan(ratio)), 0.02, 120.0))


def render_light_curve(
    frames: int,
    start_frame: int,
    end_frame: int,
    primary: ObjectSpec,
    config: RenderConfig,
    output_dir: Path,
    observer_lat: float,
    observer_lon: float,
    observer_alt_km: float,
    fov: float,
    sample_count: int,
) -> Path:
    """主物体のみを対象に、地上観測者から見たライトカーブを CSV 出力する。

    各フレームで以下を計算する:
        - range_km:        観測者と物体の距離 [km]
        - phase_angle_deg: 太陽方向と物体→観測者方向の成す角 [deg]
                            （= 太陽–物体–観測者 角。0° は観測者側の面が全面
                            照らされる満月相当、180° は逆光の新月相当）
        - in_shadow:       物体が地球の本影に入っていれば 1、そうでなければ 0
        - flux_total / flux_mean: Mitsuba で物体のみ（地球・環境光を除く）を
                                   レンダした画像の輝度総和・平均
        - rel_mag:         最大輝度を基準とする相対等級 (-2.5 log10(F/Fmax))

    flux が 0 になるのは次の 2 通り:
        1. 地球が観測者と物体の間にある（``is_earth_occluded``） = 見えない
        2. 物体が地球の影に入っている（``is_in_earth_shadow``）   = 光っていない
    この 2 つは独立で、LEO では後者（食）が軌道の ~1/3 を占める。
    出力 CSV はデフォルトで ``output_dir/light_curve.csv``。
    """
    records: List[Dict[str, Any]] = []
    fov_used: Optional[float] = None

    for frame in range(start_frame, end_frame):
        time_s = compute_frame_time(frame, frames, config, default_time_span_s(primary))
        sun_direction = resolve_sun_direction(time_s, config)
        state = compute_object_state(primary, time_s, sun_direction)
        # 地上観測者位置（緯度経度高度 → 自転角で回した ECI [km]）。地表ターゲットと
        # 同じ compute_earth_rotation_deg を使うので地球テクスチャと整合する。
        observer_km = observer_position_eci_km(time_s, config, observer_lat, observer_lon,
                                               observer_alt_km)
        observer_render = observer_km * SCALE_FACTOR
        # スラントレンジ（観測者→物体）。
        range_km = float(np.linalg.norm(state.position_km - observer_km))
        # 物体から観測者を見る方向（後方散乱の文脈では「観測方向」の逆）。
        obs_dir = observer_km - state.position_km
        obs_dir = obs_dir / np.linalg.norm(obs_dir)
        # 位相角: 太陽方向と観測方向（物体→観測者）の成す角。
        phase_angle_deg = float(np.degrees(np.arccos(np.clip(np.dot(sun_direction, obs_dir), -1.0, 1.0))))

        # 食（地球の影）判定は視線遮蔽とは独立: 見えていても光っていない場合がある。
        in_shadow = is_in_earth_shadow(state.position_km, sun_direction)

        if is_earth_occluded(observer_km, state.position_km) or in_shadow:
            # 地球による遮蔽 → 完全に見えないものとする（diffraction 等は無視）。
            # 食中 → 太陽光が当たらないので反射光も無い（地球照は無視）。
            total_flux = 0.0
            mean_flux = 0.0
        else:
            if primary.model_path:
                objects = load_external_model(
                    primary.model_path,
                    state.position_km * SCALE_FACTOR,
                    state.attitude_matrix,
                    scale=primary.scale,
                    material_name=primary.model_material,
                    keep_materials=primary.model_keep_materials,
                    prefix='primary',
                )
            else:
                objects = create_satellite(
                    state.position_km * SCALE_FACTOR,
                    state.attitude_matrix,
                    scale=primary.scale,
                    use_advanced_materials=config.lighting.use_advanced_optics,
                )
            # 観測者位置にカメラを置き、物体に向けて狭視野で撮像する。
            camera_position = observer_render
            camera_target = state.position_km * SCALE_FACTOR
            camera_up = compute_camera_up(camera_position, camera_target)
            # 画角: 指定が無ければ物体が視野に収まるよう自動（既定 satellite_scale は
            # 地球半径単位で数十 km 級になるため、固定 2° では全距離でクリップする）。
            frame_fov = resolve_light_curve_fov(
                fov, objects, np.asarray(camera_target, dtype=float),
                float(np.linalg.norm(np.asarray(camera_target) - np.asarray(camera_position))))
            if fov_used is None:
                fov_used = frame_fov
                print(f"  ライトカーブ画角: {frame_fov:.4f}° ({'指定' if fov is not None else '自動'})")
            # ライトカーブ用シーン: 地球・環境光・星空を含めず、物体だけを撮る。
            scene_dict = create_scene(
                camera_position=camera_position,
                camera_target=camera_target,
                camera_up=camera_up,
                satellite_objects=objects,
                sun_direction=sun_direction,
                earth_texture=None,
                width=config.width,
                height=config.height,
                fov=frame_fov,
                use_advanced_lighting=config.lighting.use_advanced_optics,
                sun_temperature=config.lighting.sun_temperature,
                starfield_brightness=0.0,
                include_earth=False,
                include_envmap=False,
                sample_count=sample_count,
            )
            # 画像テクスチャは (path, mtime) キャッシュ済みの Bitmap 実体へ差し替える
            # （原寸 = max_width 0 なので画は変わらない）。
            scene = mi.load_dict(preload_bitmaps(share_bsdf_textures(scene_dict)))
            image = mi.render(scene)
            total_flux, mean_flux = compute_image_flux(image)

        records.append({
            'frame': frame,
            'time_s': time_s,
            'range_km': range_km,
            'phase_angle_deg': phase_angle_deg,
            'in_shadow': 1 if in_shadow else 0,
            'flux_total': total_flux,
            'flux_mean': mean_flux,
        })

    # 相対等級の基準: フレーム列中の最大 flux を 0 mag とする
    # （絶対等級ではなく、エポック内での明暗変化を見るための相対値）。
    max_flux = max((record['flux_total'] for record in records), default=0.0)
    for record in records:
        if record['flux_total'] > 0 and max_flux > 0:
            record['rel_mag'] = -2.5 * np.log10(record['flux_total'] / max_flux)
        else:
            record['rel_mag'] = float('nan')

    output_path = output_dir / 'light_curve.csv'
    with output_path.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['frame', 'time_s', 'range_km', 'phase_angle_deg', 'in_shadow',
                         'flux_total', 'flux_mean', 'rel_mag'])
        for record in records:
            writer.writerow([
                record['frame'],
                f"{record['time_s']:.6f}",
                f"{record['range_km']:.6f}",
                f"{record['phase_angle_deg']:.6f}",
                record['in_shadow'],
                f"{record['flux_total']:.8e}",
                f"{record['flux_mean']:.8e}",
                '' if np.isnan(record['rel_mag']) else f"{record['rel_mag']:.6f}",
            ])

    return output_path


def render_frame(frame: int, total_frames: int, objects: List[ObjectSpec], config: RenderConfig, output_dir: Path,
                 trail_histories: Optional[Dict[str, List[np.ndarray]]] = None) -> None:
    """1 フレーム分の物体群状態を計算し、シーン生成から画像保存まで行う。

    処理順:
        時刻決定 → 太陽方向 → 各物体の状態（位置/速度/姿勢） → 軌跡更新
        → シーン物体辞書 → カメラ → 地球テクスチャ → Mitsuba シーン辞書
        → mi.render → トーンマップ → PNG 保存
    """
    primary = objects[0]
    time_s = compute_frame_time(frame, total_frames, config, default_time_span_s(primary))
    sun_direction = resolve_sun_direction(time_s, config)
    states = [compute_object_state(obj, time_s, sun_direction) for obj in objects]

    # 軌跡履歴に現在位置を追加
    # （ECI [km] 単位で蓄積し、描画時に SCALE_FACTOR を掛けてシーン単位に変換する）
    if trail_histories is not None and config.lighting.show_orbit:
        for state in states:
            prefix = state.spec.name.replace(' ', '_')
            if prefix not in trail_histories:
                trail_histories[prefix] = []
            trail_histories[prefix].append(state.position_km.copy())

    scene_objects = build_scene_objects(states, config, trail_histories)
    camera_position, camera_target, camera_up, fov = resolve_camera(states, config)
    earth_rotation_deg, night_emission_texture, cloud_opacity_texture = build_earth_textures(
        output_dir,
        frame,
        time_s,
        sun_direction,
        config,
    )

    scene_dict = create_scene(
        camera_position=camera_position,
        camera_target=camera_target,
        camera_up=camera_up,
        satellite_objects=scene_objects,
        sun_direction=sun_direction,
        earth_texture=config.earth.day_texture,
        width=config.width,
        height=config.height,
        fov=fov,
        use_advanced_lighting=config.lighting.use_advanced_optics,
        sun_temperature=config.lighting.sun_temperature,
        starfield_brightness=config.lighting.starfield_brightness,
        hdri_path=config.lighting.hdri_path,
        sample_count=config.lighting.sample_count,
        earth_rotation_deg=earth_rotation_deg,
        earth_night_texture=config.earth.night_texture,
        night_emission_texture=str(night_emission_texture) if night_emission_texture else None,
        earth_cloud_texture=str(cloud_opacity_texture) if cloud_opacity_texture else config.earth.cloud_texture,
        use_night_lights=config.earth.use_night_lights,
        use_clouds=config.earth.use_clouds,
        cloud_opacity=config.earth.cloud_opacity,
        use_atmosphere=config.earth.use_atmosphere,
        atmosphere_density=config.earth.atmosphere_density,
        atmosphere_radius_scale=config.earth.atmosphere_radius_scale,
    )
    # シーン辞書 → Mitsuba オブジェクト → パストレ実行。
    # load_dict の前に、地球テクスチャ・星空 HDRI などの画像を (path, mtime)
    # キャッシュ済みの Bitmap 実体へ差し替える。原寸（max_width=0）で渡すので
    # Mitsuba 側の処理経路は filename 指定時と同一 = 画は変わらない。
    scene = mi.load_dict(preload_bitmaps(share_bsdf_textures(scene_dict)))

    # 進捗表示用の高度（地心距離 - WGS84 赤道半径）。
    primary_altitude = np.linalg.norm(states[0].position_km) - EARTH_RADIUS_KM
    print(f"レンダリング中: フレーム {frame + 1}/{total_frames} (主物体高度: {primary_altitude:.1f} km)")
    image = mi.render(scene)

    # HDR レンダ結果に露光・ガンマを掛けて 8bit PNG 化。
    # apply_tonemap の出力は既に表示用（ガンマ済み）なので、mi.util.write_bitmap の
    # 既定 sRGB 変換を通すとガンマが二重にかかり宇宙背景がグレーに浮く。
    # srgb_gamma=False で「そのまま」8bit 量子化して書き出す。
    output_path = output_dir / f'frame_{frame:04d}.png'
    tonemapped = apply_tonemap(image, exposure=config.lighting.exposure, gamma=config.lighting.gamma)
    bmp = mi.Bitmap(tonemapped).convert(
        mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8, srgb_gamma=False)
    bmp.write(str(output_path))
    print(f"保存完了: {output_path}")

    # Dr.Jit は解放済みブロックを内部プールに溜め込むため、フレームごとに明示的に
    # 返却しないと巨大テクスチャ数フレーム分の RSS が積み上がる
    # （earth_beauty 3 フレームで実測 15.4 GB → 10.7 GB）。
    # 単なるアロケータへの返却なので画も所要時間も変わらない。
    del scene, image, tonemapped, bmp
    dr.flush_malloc_cache()


def print_run_summary(
    args: argparse.Namespace,
    objects: List[ObjectSpec],
    config: RenderConfig,
    output_dir: Path,
    earth_day_texture: Optional[str],
    start_frame: int,
    end_frame: int,
) -> None:
    """実行前に、主物体とレンダリング条件の要約を標準出力へ表示する。

    軌道要素・伝播モード（kepler / numerical）・摂動オン/オフ・視点モード・
    露光・テクスチャ・ライトカーブ条件などを一括ダンプし、ユーザが
    意図したジョブが投入されているかを目視確認しやすくする。
    """
    primary = objects[0]
    period_seconds = primary.orbital_elements.orbital_period
    period_minutes = period_seconds / 60.0

    print(f"{'=' * 60}")
    print('  絶対軌道・姿勢運動レンダリング')
    print(f"{'=' * 60}")
    print('主物体:')
    print(f"  名前: {primary.name}")
    print(f"  軌道高度: {primary.orbit.altitude:.1f} km")
    print(f"  軌道周期: {period_minutes:.2f} 分 ({period_seconds:.1f} 秒)")
    if primary.ephemeris is not None and primary.ephemeris.has_attitude:
        print("  姿勢: CSV quaternion (q1..q4)")
    else:
        print(f"  姿勢制御: {primary.attitude_mode}")
    prop_mode = getattr(args, 'propagator', 'kepler')
    if primary.ephemeris is not None:
        print(
            "  軌道伝播: CSV ephemeris "
            f"({primary.ephemeris.t_min:.3f}–{primary.ephemeris.t_max:.3f} s)"
        )
    elif prop_mode == 'numerical':
        perts = []
        if getattr(args, 'use_j2', False):
            perts.append('J2')
        if getattr(args, 'use_drag', False):
            perts.append(f"drag(Cd={args.drag_cd}, A/m={args.drag_a_over_m})")
        print(f"  軌道伝播: numerical (solve_ivp DOP853) [{', '.join(perts) if perts else '二体'}]")
    else:
        print("  軌道伝播: kepler（解析解）")
    if any(getattr(primary.orbit, key) != DEFAULT_ORBIT_SETTINGS[key] for key in DEFAULT_ORBIT_SETTINGS if key != 'altitude'):
        print(
            "  軌道詳細: "
            f"e={primary.orbit.eccentricity}, "
            f"i={primary.orbit.inclination}°, "
            f"RAAN={primary.orbit.raan}°, "
            f"ω={primary.orbit.arg_periapsis}°, "
            f"M0={primary.orbit.mean_anomaly}°"
        )
    if primary.model_path:
        print(f"  モデル: {primary.model_path}")

    print('\n物体数:')
    print(f"  合計: {len(objects)}")
    if len(objects) > 1:
        for obj in objects[1:]:
            print(f"  - {obj.name}: altitude={obj.orbit.altitude:.1f} km, attitude={obj.attitude_mode}")

    print('\nレンダリング設定:')
    view_camera_name = getattr(args, 'view_camera_name', None)
    view_target_name = getattr(args, 'view_target_name', None)
    if view_camera_name or view_target_name:
        cam_label = view_camera_name or f"{primary.name} (primary)"
        if view_target_name:
            print(f"  視点モード: {args.view_mode}  カメラ={cam_label} → 注視={view_target_name}")
        elif args.target_lat is not None and args.target_lon is not None:
            print(
                f"  視点モード: {args.view_mode}  カメラ={cam_label}"
                f" → 注視=lat={args.target_lat}°, lon={args.target_lon}°"
            )
        else:
            print(f"  視点モード: {args.view_mode}  カメラ={cam_label} → 注視=Earth")
    else:
        print(f"  視点モード: {args.view_mode}")
    print(f"  フレーム数: {args.frames}")
    print(f"  レンダリング範囲: {start_frame} → {end_frame}")
    print(f"  解像度: {args.width}x{args.height}")
    print(f"  サンプル数: {args.samples}")
    print(f"  地球テクスチャ: {earth_day_texture if earth_day_texture else 'なし'}")
    print(f"  出力ディレクトリ: {output_dir}")
    print(f"  軌道線表示: {'有効' if config.lighting.show_orbit else '無効'}")
    print(f"  露光補正: {args.exposure} EV")
    print(f"  ガンマ補正: {args.gamma}")
    start_t = float(getattr(args, 'start_time', 0.0) or 0.0)
    if args.duration_sec is not None:
        if args.fps is not None:
            print(f"  時間軸: duration={args.duration_sec}s, fps={args.fps}, start_t={start_t:.3f}s")
        else:
            inferred_fps = args.frames / args.duration_sec if args.duration_sec > 0 else 0.0
            print(f"  時間軸: duration={args.duration_sec}s, 推定 fps {inferred_fps:.2f}, start_t={start_t:.3f}s")
    elif start_t != 0.0:
        print(f"  時間軸: start_t={start_t:.3f}s (周期ベース)")

    if args.target_lat is not None and args.target_lon is not None:
        print('\nターゲット撮像:')
        print(f"  緯度/経度/高度: {args.target_lat}°, {args.target_lon}°, {args.target_alt} km")

    print('\n光学設定:')
    epoch_utc = getattr(args, 'epoch_utc', None)
    if epoch_utc:
        print(f"  エポック: {epoch_utc} UTC（太陽=VSOP87 / 自転角=GMST、平均分点 of date）")
    else:
        print('  エポック: 未指定（簡易モデル: t=0 で太陽=+x, グリニッジ=+x）')
    print(f"  高度な光学機能: {'有効' if args.advanced_optics else '無効'}")
    if args.advanced_optics:
        print(f"  太陽色温度: {args.sun_temperature} K")
        print(f"  星空明るさ: {args.starfield_brightness}")
    if args.sun_rotate:
        print(f"  太陽回転: 周期 {args.sun_rotate_period}s, 倍率 {args.sun_rotate_speed}")
    if args.use_clouds:
        print(f"  雲レイヤー: 有効（不透明度 {args.cloud_opacity}）")
    if args.use_night_lights:
        print('  夜間光: 有効')
    if args.use_atmosphere:
        print(f"  大気: 有効（密度 {args.atmosphere_density}, 半径倍率 {args.atmosphere_radius_scale}）")
    if args.earth_only:
        print('  地球のみ表示: 有効')

    if args.light_curve:
        print('\nライトカーブ:')
        print(f"  対象: {primary.name}")
        print(f"  観測者: {args.observer_lat}°, {args.observer_lon}°, {args.observer_alt} km")
        fov_text = '自動（物体の見かけサイズから）' if args.light_curve_fov is None else f"{args.light_curve_fov}°"
        print(f"  視野角: {fov_text}")
        print(f"  サンプル数: {args.light_curve_samples}")

    print(f"{'=' * 60}\n")


def main() -> None:
    """後方互換シム: `mrender` の動詞群へディスパッチする。

    旧来の ``python satellite_orbit.py ...`` 直接呼び出しを、新しい verb 層
    （``verbs/render.py`` / ``verbs/lightcurve.py``）へ橋渡しする。

    - `--light-curve --no-frames` → lightcurve のみ
    - `--light-curve`             → 同一 run へ frames + lightcurve
    - それ以外                    → render のみ
    既存プリセット/CLI フラグはそのまま動く。

    YAML プリセットの読み込み:
        まず `parse_known_args` で `--config` を拾い、その内容で argparse の
        デフォルトを上書きしてから `parse_args` で本パース。これにより
        優先順位「CLI 引数 > YAML > argparse デフォルト」を担保している。
    """
    from datetime import datetime as _dt

    from runs import create_run_dir
    from verbs._common import resolve_run_inputs

    parser = build_parser()
    # 1 段目: --config を取り出すために known args だけパース。
    args_pre, _ = parser.parse_known_args()
    if args_pre.config:
        # YAML をフラット化し、argparse のデフォルトに流し込む。
        parser.set_defaults(**load_yaml_config(args_pre.config))
    # 2 段目: 完全パース。CLI 明示指定は YAML より優先される。
    args = parser.parse_args()

    light_curve = bool(getattr(args, 'light_curve', False))
    no_frames = bool(getattr(args, 'no_frames', False))

    if light_curve and no_frames:
        # ライトカーブのみモード（フレーム PNG は出さない）。
        from verbs.lightcurve import run as run_lightcurve
        run_lightcurve(args)
        return

    if not light_curve:
        # 通常レンダのみ。
        from verbs.render import run as run_render
        run_render(args)
        return

    # frames + lightcurve を同じ run dir で実行する。
    # （runs/ 配下に 1 ディレクトリを切り、両方の成果物を集約する）
    config, objects, earth_day_texture = resolve_run_inputs(args)
    primary_name = getattr(args, 'primary_name', None) or objects[0].name
    run_dir = create_run_dir('render+lightcurve', args, name_hint=primary_name)
    args.output_dir = str(run_dir.path)

    print_run_summary(
        args, objects, config, run_dir.path, earth_day_texture,
        args.start_frame, args.end_frame if args.end_frame is not None else args.frames,
    )

    from verbs.render import run as run_render
    from verbs.lightcurve import run as run_lightcurve

    # マニフェスト記録: 例外時はステータスにエラー情報を残してから再送出。
    status = 'ok'
    try:
        run_render(args, run_dir=run_dir)
        run_lightcurve(args, run_dir=run_dir)
    except Exception as exc:  # noqa: BLE001
        status = f'error: {exc!r}'
        raise
    finally:
        run_dir.write_manifest(args, finished_at=_dt.now(), status=status)


if __name__ == '__main__':
    main()
