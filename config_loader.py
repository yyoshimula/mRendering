"""YAML / CLI の設定読み込みと物体仕様の組み立て。

このモジュールは satellite_orbit.py / mrender.py のエントリ部分から呼ばれ、
以下の優先順位で最終的な argparse.Namespace と ObjectSpec のリストを返す:

    CLI 引数 (--altitude 等) > YAML プリセット (--config) > _ARG_DEFAULTS

YAML フラット化の規則:
    - YAML はネスト構造 (primary:, camera:, earth: ...) で書ける
    - load_yaml_config() でセクションを 1 段だけフラット化し、
      argparse の dest 名と一致するキーに整列する
    - そのため YAML 側のキー名は argparse の `dest` に合わせるか、
      下記の merge_*_section が変換規則を持っている必要がある

物体仕様の構築フロー:
    1) argparse.Namespace から resolve_primary_object() で主物体を生成
    2) YAML の `objects:` (リスト) を resolve_object_specs() で追加物体に変換
    3) 各 ObjectSpec は orbital_elements / propagator / ephemeris を保持

Mitsuba には触れない。シーン辞書の組み立ては scene_builder.py / scene_objects.py 側。
"""

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

from orbit_mechanics import (
    parse_epoch_jd,
    OrbitalElements,
    EARTH_RADIUS_KM,
    NumericalPropagator,
    CsvEphemeris,
)
from render_config import (
    DEFAULT_ORBIT_SETTINGS,
    SECTION_ALIASES,
    CameraConfig,
    CameraOverrides,
    EarthConfig,
    LightingConfig,
    AnimationConfig,
    RenderConfig,
    OrbitSettings,
    ObjectSpec,
)


def normalize_object_config(item: Dict[str, Any]) -> Dict[str, Any]:
    """YAML の 1 物体定義を、内部で扱いやすいフラットな辞書へ正規化する。

    - キー名のハイフン (`-`) はアンダースコア (`_`) に置換される
    - 値が dict の場合は 1 段だけフラット化される
      (例: `model: {path: foo.obj}` → `path: foo.obj` に展開)
    """
    normalized: Dict[str, Any] = {}
    for key, value in item.items():
        normalized_key = key.replace('-', '_')
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                normalized[sub_key.replace('-', '_')] = sub_value
        else:
            normalized[normalized_key] = value
    return normalized


def normalize_key(key: str) -> str:
    """YAML/CLI 間で揺れやすいキー名をアンダースコア形式へ揃える。"""
    return key.replace('-', '_')


def merge_primary_section(target: Dict[str, Any], data: Dict[str, Any]) -> None:
    """`primary:` セクションを CLI 互換の key 名へ展開する。

    YAML 側の組織用キー (name / scale / model.path …) を、argparse dest 名
    (primary_name / satellite_scale / satellite_model …) に変換する。
    変換規則の追加はここに集約される。
    """
    for key, value in data.items():
        normalized_key = normalize_key(key)
        if normalized_key == 'name':
            target['primary_name'] = value
        elif normalized_key == 'attitude_mode':
            target['attitude_mode'] = value
        elif normalized_key == 'scale':
            target['satellite_scale'] = value
        elif normalized_key == 'orbit' and isinstance(value, dict):
            # primary.orbit.{altitude, inclination, ...} はそのままトップレベルへ
            for orbit_key, orbit_value in value.items():
                target[normalize_key(orbit_key)] = orbit_value
        elif normalized_key == 'model' and isinstance(value, dict):
            # primary.model.{path,material,keep_materials} → satellite_* に変換
            for model_key, model_value in value.items():
                mapped_key = {
                    'path': 'satellite_model',
                    'material': 'satellite_model_material',
                    'keep_materials': 'satellite_model_keep_materials',
                }.get(normalize_key(model_key), f"satellite_{normalize_key(model_key)}")
                target[mapped_key] = model_value
        else:
            mapped_key = {
                'model_path': 'satellite_model',
                'model_material': 'satellite_model_material',
                'model_keep_materials': 'satellite_model_keep_materials',
            }.get(normalized_key, normalized_key)
            target[mapped_key] = value


def merge_camera_section(target: Dict[str, Any], data: Dict[str, Any]) -> None:
    """`camera:` セクションを CLI 互換の key 名へ展開する。

    YAML の origin/target/fov/up を camera_* prefix 付きに変換する。
    """
    for key, value in data.items():
        normalized_key = normalize_key(key)
        mapped_key = {
            'origin': 'camera_origin',
            'target': 'camera_target',
            'fov': 'camera_fov',
            'up': 'camera_up',
        }.get(normalized_key, normalized_key)
        target[mapped_key] = value


def merge_earth_section(target: Dict[str, Any], data: Dict[str, Any]) -> None:
    """`earth:` セクションを CLI 互換の key 名へ展開する。

    day/night/cloud_texture を earth_*_texture に統一する。
    """
    for key, value in data.items():
        normalized_key = normalize_key(key)
        mapped_key = {
            'day_texture': 'earth_day_texture',
            'night_texture': 'earth_night_texture',
            'cloud_texture': 'earth_cloud_texture',
        }.get(normalized_key, normalized_key)
        target[mapped_key] = value


def merge_generic_section(target: Dict[str, Any], data: Dict[str, Any]) -> None:
    """単純なセクションをそのままフラット化する (キー名のみ正規化)。"""
    for key, value in data.items():
        target[normalize_key(key)] = value


def load_yaml_config(paths: List[str]) -> dict:
    """YAML プリセットを読み込み、argparse の default に流し込める形へ整える。

    対応セクション: `primary / objects / rendering / camera / earth / lighting / animation`。

    挙動:
        - 複数ファイル指定時は順に処理し、後の方が優先される (上書き)
        - `objects:` は配列なのでフラット化せずそのまま `merged['objects']` に保持
        - dict でない値はトップレベルキーとしてそのまま追加
        - dict はセクション種別に応じた merge_*_section() に委譲

    Returns:
        argparse.set_defaults(**merged) に渡せるフラット辞書。
    """
    merged: Dict[str, Any] = {}
    for path in paths:
        with open(path) as handle:
            data = yaml.safe_load(handle)
        if data is None:
            continue
        for key, value in data.items():
            normalized_key = SECTION_ALIASES.get(normalize_key(key), normalize_key(key))
            if normalized_key == 'objects':
                merged['objects'] = value
            elif not isinstance(value, dict):
                merged[normalized_key] = value
            elif normalized_key == 'primary':
                merge_primary_section(merged, value)
            elif normalized_key == 'camera':
                merge_camera_section(merged, value)
            elif normalized_key == 'earth':
                merge_earth_section(merged, value)
            else:
                merge_generic_section(merged, value)
    return merged


def load_flat_yaml_config(paths: List[Any]) -> Dict[str, Any]:
    """YAML プリセットを「素朴な 1 段フラット化」で読み込む。

    relative / rotation verb 用のローダ。`load_yaml_config()` と違いセクション
    名の別名解決 (SECTION_ALIASES) や `primary:` 等の特殊変換は一切行わず、
    dict 値のセクションを 1 段だけ展開して argparse の dest 名
    （アンダースコア区切り）に揃えるだけ。

        rendering: {frames: 30}   →  {'frames': 30}
        mode: tumble              →  {'mode': 'tumble'}

    複数ファイル指定時は後のファイルが優先（dict の上書きマージ）。
    優先順位は呼び出し側の parse_args で CLI 引数 > YAML > argparse デフォルト。

    実装がここにあるのは Mitsuba 非依存だから。レンダラ本体
    (`relative_motion` / `simple_rotation`) と GUI サーバー (`gui_server`) の
    両方がこの 1 実装を共有する。

    Args:
        paths: YAML ファイルパス（str でも Path でも可）。

    Returns:
        argparse.set_defaults(**merged) に渡せるフラット辞書。
    """
    merged: Dict[str, Any] = {}
    for path in paths:
        with open(path) as handle:
            data = yaml.safe_load(handle)
        if data is None:
            continue
        for key, value in data.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    merged[normalize_key(sub_key)] = sub_value
            else:
                merged[normalize_key(key)] = value
    return merged


# argparse のデフォルト値辞書。
# build_parser() の中で `parser.set_defaults(**_ARG_DEFAULTS)` 経由で適用される。
# 優先順位: CLI 引数 > YAML > _ARG_DEFAULTS の順で上書きされる。
# キー名は argparse の dest 名と一致させること (YAML 側もこの名前に正規化される)。
_ARG_DEFAULTS: Dict[str, Any] = {
    # workflow (フレーム数・出力先・解像度・サンプル数)
    'frames': 60,
    'start_frame': 0,
    'end_frame': None,
    'duration_sec': None,
    'fps': None,
    'start_time': 0.0,
    'epoch_utc': None,        # ISO 8601 UTC。指定すると太陽方向=VSOP87、自転角=GMST（実時刻）
    'output_dir': 'output',
    'width': 1920,
    'height': 1080,
    'samples': 128,            # spp (パストレーシングのピクセル毎サンプル数)
    'no_frames': False,
    # primary object (主物体の軌道・姿勢・モデル)
    'altitude': DEFAULT_ORBIT_SETTINGS['altitude'],
    'attitude_mode': 'nadir',
    'satellite_scale': 0.002,  # シーン単位 (地球半径=1.0)
    'satellite_model': None,
    'satellite_model_material': None,
    'satellite_model_keep_materials': False,
    'csv_path': None,
    'orbit_speed': 1.0,
    'propagator': 'kepler',    # 'kepler' or 'numerical' (J2/drag)
    'use_j2': False,
    'use_drag': False,
    'drag_cd': 2.2,
    'drag_a_over_m': 0.01,
    # earth (地球テクスチャ・雲・夜光・大気・自転)
    'earth_texture': 'earth_texture.jpg',
    'earth_day_texture': None,
    'earth_night_texture': None,
    'earth_cloud_texture': None,
    'use_night_lights': False,
    'use_clouds': False,
    'cloud_opacity': 0.5,
    'night_mask_res': 1024,
    'night_terminator_softness': 0.02,
    'use_atmosphere': False,
    'atmosphere_density': 0.02,
    'atmosphere_radius_scale': 1.03,
    'earth_only': False,
    'earth_rotation': True,
    'earth_rotation_period_hours': 23.934,  # 恒星日 [h]
    'earth_rotation_speed': 1.0,
    # lighting (太陽・星空・HDRI・トーンマップ)
    'advanced_optics': False,
    'sun_temperature': 5778.0,    # 太陽の有効温度 [K]
    'sun_angle': None,
    'sun_rotate': False,
    'sun_rotate_period': 60.0,
    'sun_rotate_speed': 1.0,
    'starfield_brightness': 0.01,
    'hdri_path': None,
    'show_orbit': False,
    'orbit_line_radius': 0.005,           # 軌跡シリンダーの半径 (シーン単位、地球半径=1.0)
    'orbit_line_color': [0.2, 0.6, 1.0],  # 軌跡 RGB 色 [0..1]
    'orbit_line_glow': 0.5,               # 軌跡の自発光倍率 (0 で発光なし、>0 で area emitter 付与)
    # camera (ビュー切替・チェース距離・上書き値)
    'view_mode': 'inertial',
    'chase_back': 0.05,
    'chase_out': 0.05,
    'inertial_fixed': True,
    'inertial_distance': 5.5,
    'camera_origin': None,
    'camera_target': None,
    'camera_fov': None,
    'camera_up': None,
    'target_lat': None,
    'target_lon': None,
    'target_alt': 0.0,
    'view_camera_name': None,
    'view_target_name': None,
    # video output (ffmpeg 連携)
    'make_video': False,
    'video_fps': 30.0,
    'video_name': 'output.mp4',
    # observer / light curve (光度曲線解析)
    'light_curve': False,
    'light_curve_fov': None,   # [deg]。None で物体の見かけサイズから自動（クリップ防止）
    'light_curve_samples': 64,
    'observer_lat': 35.0,
    'observer_lon': 135.0,
    'observer_alt': 0.0,
    # tone map
    'exposure': 0.0,    # [stops]、+1 で 2 倍明るくなる
    'gamma': 2.2,       # sRGB 近似ガンマ
}


def build_parser() -> argparse.ArgumentParser:
    """CLI インターフェースを構築する。設定は YAML プリセットで指定する。

    `--config` 以外の個別フラグはここでは追加せず、_ARG_DEFAULTS のみを set_defaults で
    適用する。実際の上書きは呼び出し側で
        args, _ = parser.parse_known_args()
        if args.config: parser.set_defaults(**load_yaml_config(args.config))
        args = parser.parse_args()
    のように 2 段階で行う想定。
    """
    parser = argparse.ArgumentParser(
        description='地球周回物体の絶対軌道と姿勢運動をレンダリングする',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python satellite_orbit.py --config presets/iss.yaml
  python satellite_orbit.py --config presets/multi_object.yaml
        """
    )
    parser.add_argument('--config', nargs='+', default=[],
                        help='YAMLプリセット（複数指定可、後のファイルが優先）')
    parser.set_defaults(**_ARG_DEFAULTS)
    return parser


def orbit_settings_from_mapping(source: Dict[str, Any], fallback: OrbitSettings) -> OrbitSettings:
    """辞書から軌道要素を取り出し、未指定分は fallback で補完する。

    Args:
        source:   altitude / eccentricity / inclination / raan / arg_periapsis /
                  mean_anomaly のいずれか (または全部) を含む辞書
        fallback: 欠損キーに使うデフォルト OrbitSettings (deg/km 単位)

    Returns:
        OrbitSettings (角度は deg、altitude は km)
    """
    return OrbitSettings(
        altitude=float(source.get('altitude', fallback.altitude)),
        eccentricity=float(source.get('eccentricity', fallback.eccentricity)),
        inclination=float(source.get('inclination', fallback.inclination)),
        raan=float(source.get('raan', fallback.raan)),
        arg_periapsis=float(source.get('arg_periapsis', fallback.arg_periapsis)),
        mean_anomaly=float(source.get('mean_anomaly', fallback.mean_anomaly)),
    )


def build_orbital_elements(settings: OrbitSettings) -> OrbitalElements:
    """度・高度ベースの設定値から `OrbitalElements` を生成する。

    変換:
        a [km]   = R_earth + altitude
        角度 [rad] = np.radians(deg)
    """
    return OrbitalElements(
        semi_major_axis=EARTH_RADIUS_KM + settings.altitude,
        eccentricity=settings.eccentricity,
        inclination=np.radians(settings.inclination),
        raan=np.radians(settings.raan),
        arg_periapsis=np.radians(settings.arg_periapsis),
        mean_anomaly_0=np.radians(settings.mean_anomaly),
    )


def resolve_primary_object(args: argparse.Namespace) -> ObjectSpec:
    """CLI から主物体の仕様を組み立てる。

    動作:
        1. トップレベルの altitude / eccentricity / ... から OrbitSettings を作る
           (YAML の primary: セクションは merge_primary_section でここに展開済み)
        2. CSV 軌道 (`csv_path`) があれば CsvEphemeris を生成
           なければ propagator='numerical' のとき NumericalPropagator を生成
           どちらもなければ Kepler 解析解で進める (propagator/ephemeris=None)
        3. ObjectSpec を返す
    """
    fallback = OrbitSettings(**DEFAULT_ORBIT_SETTINGS)
    top_level_source = {
        'altitude': args.altitude,
        'eccentricity': getattr(args, 'eccentricity', DEFAULT_ORBIT_SETTINGS['eccentricity']),
        'inclination': getattr(args, 'inclination', DEFAULT_ORBIT_SETTINGS['inclination']),
        'raan': getattr(args, 'raan', DEFAULT_ORBIT_SETTINGS['raan']),
        'arg_periapsis': getattr(args, 'arg_periapsis', DEFAULT_ORBIT_SETTINGS['arg_periapsis']),
        'mean_anomaly': getattr(args, 'mean_anomaly', DEFAULT_ORBIT_SETTINGS['mean_anomaly']),
    }
    orbit = orbit_settings_from_mapping(top_level_source, fallback)
    oe = build_orbital_elements(orbit)

    # 軌道時系列の供給源を選ぶ: CSV > 数値伝播 > Kepler 解析解
    propagator = None
    ephemeris = None
    csv_path = getattr(args, 'csv_path', None)
    if csv_path:
        ephemeris = CsvEphemeris(csv_path)
    elif getattr(args, 'propagator', 'kepler') == 'numerical':
        propagator = NumericalPropagator(
            oe,
            use_j2=getattr(args, 'use_j2', False),
            use_drag=getattr(args, 'use_drag', False),
            drag_cd=getattr(args, 'drag_cd', 2.2),
            drag_a_over_m=getattr(args, 'drag_a_over_m', 0.01),
        )

    return ObjectSpec(
        name=getattr(args, 'primary_name', 'primary'),
        orbital_elements=oe,
        orbit=orbit,
        attitude_mode=args.attitude_mode,
        scale=args.satellite_scale,
        model_path=args.satellite_model,
        model_material=args.satellite_model_material,
        model_keep_materials=args.satellite_model_keep_materials,
        propagator=propagator,
        ephemeris=ephemeris,
    )


def resolve_object_specs(args: argparse.Namespace) -> List[ObjectSpec]:
    """主物体と、YAML の `objects:` で追加された物体群を最終的な仕様へ変換する。

    `objects:` は YAML 側で list 形式に書く想定。各要素はフラット化された後、
    主物体の軌道設定を fallback として欠損値が補完される。
    """
    primary = resolve_primary_object(args)
    object_items = getattr(args, 'objects', None)
    if not object_items:
        return [primary]

    specs: List[ObjectSpec] = []
    for index, raw_item in enumerate(object_items):
        if not isinstance(raw_item, dict):
            raise ValueError(f'objects[{index}] は dict である必要があります。')
        item = normalize_object_config(raw_item)
        # 個別物体の軌道指定が無い項目は primary の軌道設定で埋める
        orbit = orbit_settings_from_mapping(item, primary.orbit)
        oe = build_orbital_elements(orbit)

        # 各物体ごとに CSV / 数値伝播器を作る (resolve_primary_object と同じ判定ロジック)
        obj_propagator = None
        obj_ephemeris = None
        obj_csv = item.get('csv_path', None)
        if obj_csv:
            obj_ephemeris = CsvEphemeris(obj_csv)
        elif getattr(args, 'propagator', 'kepler') == 'numerical':
            obj_propagator = NumericalPropagator(
                oe,
                use_j2=getattr(args, 'use_j2', False),
                use_drag=getattr(args, 'use_drag', False),
                drag_cd=getattr(args, 'drag_cd', 2.2),
                drag_a_over_m=getattr(args, 'drag_a_over_m', 0.01),
            )

        specs.append(ObjectSpec(
            name=str(item.get('name', f'object_{index + 1}')),
            orbital_elements=oe,
            orbit=orbit,
            attitude_mode=str(item.get('attitude_mode', args.attitude_mode)),
            scale=float(item.get('scale', args.satellite_scale)),
            # YAML では model_path / path / model_material / material のどちらでも書ける
            model_path=item.get('model_path', item.get('path', None)),
            model_material=item.get('model_material', item.get('material', None)),
            model_keep_materials=bool(item.get('model_keep_materials', item.get('keep_materials', False))),
            propagator=obj_propagator,
            ephemeris=obj_ephemeris,
        ))

    return specs


def resolve_earth_day_texture(args: argparse.Namespace) -> Optional[str]:
    """昼側の地球テクスチャを決定する。専用指定があればそちらを優先する。

    優先順: `--earth-day-texture` > `--earth-texture` (旧式・後方互換)
    """
    return args.earth_day_texture if args.earth_day_texture else args.earth_texture


def build_render_config(args: argparse.Namespace, earth_day_texture: Optional[str]) -> RenderConfig:
    """CLI/YAML の共通設定からレンダリング用設定オブジェクトを作る。

    args (argparse.Namespace) のフラットなキーを、用途別の dataclass へ詰め替える。
    Mitsuba シーン辞書を直接生成するのは scene_builder.create_scene() 側。
    """
    return RenderConfig(
        width=args.width,
        height=args.height,
        camera=CameraConfig(
            view_mode=args.view_mode,
            target_lat=args.target_lat,
            target_lon=args.target_lon,
            target_alt_km=args.target_alt,
            chase_back=args.chase_back,
            chase_out=args.chase_out,
            inertial_fixed=args.inertial_fixed,
            inertial_distance=args.inertial_distance,
            overrides=CameraOverrides(
                origin=args.camera_origin,
                target=args.camera_target,
                fov=args.camera_fov,
                up=args.camera_up,
            ),
            view_camera_name=args.view_camera_name,
            view_target_name=args.view_target_name,
        ),
        earth=EarthConfig(
            day_texture=earth_day_texture,
            night_texture=args.earth_night_texture,
            cloud_texture=args.earth_cloud_texture,
            use_night_lights=args.use_night_lights,
            use_clouds=args.use_clouds,
            cloud_opacity=args.cloud_opacity,
            night_mask_res=args.night_mask_res,
            night_terminator_softness=args.night_terminator_softness,
            use_atmosphere=args.use_atmosphere,
            atmosphere_density=args.atmosphere_density,
            atmosphere_radius_scale=args.atmosphere_radius_scale,
            earth_rotation=args.earth_rotation,
            earth_rotation_period_hours=args.earth_rotation_period_hours,
            earth_rotation_speed=args.earth_rotation_speed,
            earth_only=args.earth_only,
        ),
        lighting=LightingConfig(
            use_advanced_optics=args.advanced_optics,
            sun_temperature=args.sun_temperature,
            sun_angle=args.sun_angle,
            sun_rotate=args.sun_rotate,
            sun_rotate_period_s=args.sun_rotate_period,
            sun_rotate_speed=args.sun_rotate_speed,
            starfield_brightness=args.starfield_brightness,
            hdri_path=args.hdri_path,
            show_orbit=args.show_orbit,
            orbit_line_radius=float(args.orbit_line_radius),
            orbit_line_color=tuple(args.orbit_line_color),
            orbit_line_glow=float(args.orbit_line_glow),
            sample_count=args.samples,
            exposure=args.exposure,
            gamma=args.gamma,
        ),
        animation=AnimationConfig(
            orbit_speed=args.orbit_speed,
            duration_sec=args.duration_sec,
            fps=args.fps,
            start_time=float(getattr(args, 'start_time', 0.0) or 0.0),
            epoch_jd=(parse_epoch_jd(args.epoch_utc)
                      if getattr(args, 'epoch_utc', None) else None),
        ),
    )


def validate_model_paths(parser: argparse.ArgumentParser, objects: List[ObjectSpec]) -> None:
    """外部モデルの存在と拡張子を事前に検証する。

    ファイル不在 / 未対応拡張子の場合は parser.error() で即時終了させる。
    対応拡張子: .obj / .ply / .glb / .gltf
    """
    for obj in objects:
        if obj.model_path is None:
            continue
        path = Path(obj.model_path)
        if not path.exists():
            parser.error(f"モデルファイルが見つかりません ({obj.name}): {obj.model_path}")
        if path.suffix.lower() not in ('.obj', '.ply', '.glb', '.gltf'):
            parser.error(f"未対応のモデル形式です ({obj.name}): {obj.model_path}")
