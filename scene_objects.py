"""
衛星・デブリの3Dオブジェクト生成モジュール

手続き的3Dモデル（衛星本体、ソーラーパネル、アンテナ、デブリ）の生成、
外部3Dモデル（OBJ/PLY/glTF）の読み込み、軌道線・相対ベクトル線の描画。

責務:
    - 軌道周回物体 (衛星 / デブリ) を Mitsuba シーン辞書に追加できる形 ({key: shape_dict})
      で生成して返す
    - 外部 3D モデル (OBJ/PLY/GLB) を読み込み、同じ辞書形式に統一する
    - 軌跡 (trail) や 2 物体間の相対ベクトル可視化用シリンダー列を生成

呼び出し元: satellite_orbit.py / relative_motion.py / simple_rotation.py
返した辞書は scene_builder.create_scene(satellite_objects=...) 経由でシーンに合流する。

スケール / 単位:
    - position はシーン単位 (地球半径=1.0)。実 km 値からの変換は
      orbit_mechanics.SCALE_FACTOR を介する
    - 衛星 procedural モデルは scale パラメータ (シーン単位) を基準に倍率で寸法を決める
      代表値: scale=0.001 で「ISS の 1/2 本体長」程度に見える
    - 本体: 1.5 x 1.5 x 2.0 (XYZ 倍率 × scale)
      ソーラーパネル: 2.5 x 0.05 x 1.5 を本体の左右 ±3.0×scale に配置
      アンテナ: 直径 0.6×scale、高さ 1.5×scale を本体上 +2.5×scale に配置

材質 (BSDF) 解決:
    - 文字列 (例: 'gold', 'solar_panel') → Mitsuba BSDF dict は
      `resolve_material_by_name()` を介する
    - 利用可能な名前は MaterialLibrary + create_*_bsdf の実装に対応 (下記 dict 参照)
"""

import numpy as np
from pathlib import Path
from typing import Tuple, Optional, Dict, Any

import mitsuba as mi

from optical_materials import (
    MaterialLibrary,
    create_carbon_composite_bsdf,
    create_white_paint_bsdf,
    create_kapton_mli_bsdf,
)
from orbit_mechanics import compute_orbital_position, SCALE_FACTOR


def resolve_material_by_name(name: str) -> Dict[str, Any]:
    """
    材質名文字列からMitsuba BSDF辞書を返すヘルパー

    YAML/CLI で `material: gold` のように指定された材質名を、
    optical_materials.py で定義した BSDF dict に変換する。
    対応する名前はソース内の material_map のキー (9 種) を参照。

    Args:
        name: 材質名 (例: 'aluminum_brushed', 'gold', 'solar_panel', ...)

    Returns:
        Mitsuba BSDF辞書

    Raises:
        ValueError: name が material_map に無い場合 (利用可能名を併記)
    """
    mat_lib = MaterialLibrary()
    material_map: Dict[str, Any] = {
        'aluminum_brushed': mat_lib.get_satellite_body_material('brushed'),
        'aluminum_polished': mat_lib.get_satellite_body_material('polished'),
        'gold': mat_lib.get_antenna_material(),
        'solar_panel': mat_lib.get_solar_panel_material(),
        'thermal_blanket': mat_lib.get_thermal_blanket_material(),
        'radiator': mat_lib.get_radiator_material(),
        'carbon_composite': create_carbon_composite_bsdf(),
        'white_paint': create_white_paint_bsdf(),
        'kapton_mli': create_kapton_mli_bsdf(),
    }
    if name not in material_map:
        available = ', '.join(sorted(material_map.keys()))
        raise ValueError(f"未知の材質名: '{name}'  (利用可能: {available})")
    return material_map[name]


def load_external_model(
    filepath: str,
    position: np.ndarray,
    attitude_matrix: np.ndarray,
    scale: float = 0.001,
    material_name: Optional[str] = None,
    keep_materials: bool = False,
    prefix: str = 'ext',
) -> Dict[str, Any]:
    """
    外部3Dモデル（OBJ/PLY）を読み込み、create_satellite()/create_debris()と同じdict形式で返す

    フロー:
        1. .glb/.gltf は trimesh で .obj に変換 (中間ファイルを同ディレクトリに作る)
        2. 拡張子から Mitsuba shape type ('obj' or 'ply') を決定
        3. 変換行列 = translate(position) @ rotate(attitude_matrix) @ scale(scale)
           を組み立てる (Mitsuba は列ベクトル右乗算なので順序に注意)
        4. keep_materials=False の時のみ BSDF を付与
           - material_name 指定 → resolve_material_by_name で解決
           - 未指定 → デフォルトで aluminum_brushed
        5. {f'{prefix}_mesh': mesh_dict} の形で返す

    Args:
        filepath: モデルファイルパス (.obj / .ply / .glb / .gltf)
        position: 位置ベクトル [シーン単位]
        attitude_matrix: 姿勢行列 (3x3、Body→World)
        scale: 均一スケール（シーン単位）
        material_name: 材質名（Noneの場合はデフォルトのaluminum_brushed）
        keep_materials: Trueの場合、モデル埋め込みMTL材質を使用（BSDFを付与しない）
        prefix: 辞書キーのプレフィックス (multi-object 時に衝突回避)

    Returns:
        {'{prefix}_mesh': {'type': 'obj'|'ply', 'filename': ..., 'to_world': ..., 'bsdf': ...}}
    """
    path = Path(filepath)
    ext = path.suffix.lower()

    # glTF/GLB → OBJ 変換（trimesh経由）
    # Mitsuba 3 は OBJ/PLY の組み込みパーサしか持たないため、glTF はここで一度 OBJ に落とす
    if ext in ('.glb', '.gltf'):
        import trimesh
        scene_or_mesh = trimesh.load(str(path))
        if isinstance(scene_or_mesh, trimesh.Scene):
            meshes = [g for g in scene_or_mesh.geometry.values()
                      if isinstance(g, trimesh.Trimesh)]
            if not meshes:
                raise ValueError(f"GLBファイルにメッシュが含まれていません: {filepath}")
            mesh = trimesh.util.concatenate(meshes)
        else:
            mesh = scene_or_mesh
        obj_path = path.with_suffix('.obj')
        mesh.export(str(obj_path), file_type='obj', include_normals=False)
        path = obj_path
        ext = '.obj'
        print(f"  [trimesh] {filepath} → {obj_path}")

    if ext == '.obj':
        mesh_type = 'obj'
    elif ext == '.ply':
        mesh_type = 'ply'
    else:
        raise ValueError(f"未対応のモデル形式: '{ext}' (.obj/.ply/.glb/.gltf のみ対応)")

    # Transform: translate @ rotate @ scale（既存パイプラインと同一）
    # Mitsuba は to_world に渡された行列をローカル→ワールド変換として使う
    transform = mi.ScalarTransform4f.translate(position)
    rotation_matrix = np.eye(4)
    rotation_matrix[:3, :3] = attitude_matrix
    transform = transform @ mi.ScalarTransform4f(rotation_matrix)
    transform = transform @ mi.ScalarTransform4f.scale([scale, scale, scale])

    mesh_dict: Dict[str, Any] = {
        'type': mesh_type,
        'filename': str(path),
        'to_world': transform,
    }

    # keep_materials=True なら OBJ の MTL を尊重 (BSDF を付けないと Mitsuba は MTL を読む)
    if not keep_materials:
        if material_name is not None:
            mesh_dict['bsdf'] = resolve_material_by_name(material_name)
        else:
            mesh_dict['bsdf'] = resolve_material_by_name('aluminum_brushed')

    return {f'{prefix}_mesh': mesh_dict}


def create_satellite(position: np.ndarray, attitude_matrix: np.ndarray, scale: float = 0.001,
                    use_advanced_materials: bool = True) -> dict:
    """
    リアルな衛星3Dモデルを作成

    procedural な衛星 (本体 + 両翼ソーラーパネル + アンテナ) を辞書で返す。
    寸法はすべて scale (シーン単位) を基準に倍率で決まる:

        本体           : cube  1.5 x 1.5 x 2.0  (中央)
        ソーラーパネル : cube  2.5 x 0.05 x 1.5 (本体左右 ±3.0)
        アンテナ       : cyl   0.3 x 0.3 x 1.5  (本体上 +2.5、円筒は Z 軸が長軸)

    use_advanced_materials=True で MaterialLibrary 由来の物理 BSDF
    (鏡面 Al, Au アンテナ, ソーラーパネル) を使う。False は単純な diffuse/conductor。

    Args:
        position: 衛星位置 [シーン単位]
        attitude_matrix: 姿勢行列 (3x3、Body→World)
        scale: 衛星のスケール（シーン単位、地球半径=1.0）
        use_advanced_materials: 高度な材質を使用

    Returns:
        Mitsubaシーン辞書（複数のオブジェクトを 1 dict にまとめたもの）
    """
    objects = {}

    # 材質ライブラリ
    mat_lib = MaterialLibrary()

    # 姿勢行列からMitsubaのTransform4fを生成
    # transform = T(pos) @ R(attitude) を共通の親変換とし、各部品はさらに局所変換を被せる
    transform = mi.ScalarTransform4f.translate(position)
    # 姿勢行列を適用
    rotation_matrix = np.eye(4)
    rotation_matrix[:3, :3] = attitude_matrix
    transform = transform @ mi.ScalarTransform4f(rotation_matrix)

    # 1. 衛星本体（箱型）
    body_scale = mi.ScalarTransform4f.scale([scale * 1.5, scale * 1.5, scale * 2.0])
    if use_advanced_materials:
        body_bsdf = mat_lib.get_satellite_body_material('brushed')
    else:
        body_bsdf = {'type': 'conductor', 'material': 'Al'}

    objects['satellite_body'] = {
        'type': 'cube',
        'to_world': transform @ body_scale,
        'bsdf': body_bsdf
    }

    # 2. ソーラーパネル（左）
    # 本体の左 (-X 方向) に伸びる薄い板。Y 厚を 0.05 と極小にして板状にする
    panel_left_transform = mi.ScalarTransform4f.translate([-scale * 3.0, 0, 0])
    panel_scale = mi.ScalarTransform4f.scale([scale * 2.5, scale * 0.05, scale * 1.5])
    if use_advanced_materials:
        panel_bsdf = mat_lib.get_solar_panel_material()
    else:
        panel_bsdf = {'type': 'diffuse', 'reflectance': {'type': 'rgb', 'value': [0.05, 0.05, 0.2]}}

    objects['solar_panel_left'] = {
        'type': 'cube',
        'to_world': transform @ panel_left_transform @ panel_scale,
        'bsdf': panel_bsdf
    }

    # 3. ソーラーパネル（右）
    panel_right_transform = mi.ScalarTransform4f.translate([scale * 3.0, 0, 0])
    objects['solar_panel_right'] = {
        'type': 'cube',
        'to_world': transform @ panel_right_transform @ panel_scale,
        'bsdf': panel_bsdf
    }

    # 4. アンテナ（円筒形）
    # cylinder は既定で Z 軸方向、高さ 2 / 半径 1。scale で実寸に縮める
    antenna_transform = mi.ScalarTransform4f.translate([0, 0, scale * 2.5])
    antenna_scale = mi.ScalarTransform4f.scale([scale * 0.3, scale * 0.3, scale * 1.5])
    if use_advanced_materials:
        antenna_bsdf = mat_lib.get_antenna_material()
    else:
        antenna_bsdf = {'type': 'conductor', 'material': 'Au'}

    objects['antenna'] = {
        'type': 'cylinder',
        'to_world': transform @ antenna_transform @ antenna_scale,
        'bsdf': antenna_bsdf
    }

    return objects


def create_debris(position: np.ndarray, attitude_matrix: np.ndarray, scale: float = 0.001,
                 use_advanced_materials: bool = True,
                 chief_style: str = 'full') -> dict:
    """
    デブリ（箱＋ウイング）モデルを作成

    chief_style の 3 モード:
        - 'marker'  : 単一の赤い小球 (位置確認用、姿勢無視)
        - 'boxwing' : 小型 box + 左右 wing (procedural 簡易モデル)
        - 'full'    : 'boxwing' より大きい本体 + 1 翼 (デフォルト)
    """
    mat_lib = MaterialLibrary()

    if chief_style == 'marker':
        # 位置マーカ用: 姿勢を無視した最小サイズの赤球。デバッグ・可視化向け
        body_bsdf = {
            'type': 'diffuse',
            'reflectance': {'type': 'rgb', 'value': [0.95, 0.3, 0.25]}
        }
        marker_radius = max(scale * 0.25, 2.5e-4)
        return {
            'debris_body': {
                'type': 'sphere',
                'center': position.tolist(),
                'radius': marker_radius,
                'bsdf': body_bsdf
            }
        }

    if chief_style == 'boxwing':
        # 小型 box + 両翼パネル (左右対称)。色は固定 (グレー本体 + 紺パネル)
        body_bsdf = {
            'type': 'diffuse',
            'reflectance': {'type': 'rgb', 'value': [0.75, 0.75, 0.78]}
        }
        wing_bsdf = {
            'type': 'diffuse',
            'reflectance': {'type': 'rgb', 'value': [0.12, 0.22, 0.4]}
        }
        transform = mi.ScalarTransform4f.translate(position)
        rotation_matrix = np.eye(4)
        rotation_matrix[:3, :3] = attitude_matrix
        transform = transform @ mi.ScalarTransform4f(rotation_matrix)

        body_scale = mi.ScalarTransform4f.scale([scale * 0.45, scale * 0.45, scale * 0.60])
        wing_scale = mi.ScalarTransform4f.scale([scale * 1.00, scale * 0.10, scale * 0.35])
        wing_left = mi.ScalarTransform4f.translate([-scale * 0.70, 0, 0])
        wing_right = mi.ScalarTransform4f.translate([scale * 0.70, 0, 0])
        return {
            'debris_body': {
                'type': 'cube',
                'to_world': transform @ body_scale,
                'bsdf': body_bsdf
            },
            'debris_wing_left': {
                'type': 'cube',
                'to_world': transform @ wing_left @ wing_scale,
                'bsdf': wing_bsdf
            },
            'debris_wing_right': {
                'type': 'cube',
                'to_world': transform @ wing_right @ wing_scale,
                'bsdf': wing_bsdf
            }
        }

    # 'full' (デフォルト): 大型本体 + 片翼 (非対称デブリ想定)
    if use_advanced_materials:
        body_bsdf = mat_lib.get_aluminum()
        wing_bsdf = mat_lib.get_solar_panel()
    else:
        body_bsdf = {
            'type': 'diffuse',
            'reflectance': {'type': 'rgb', 'value': [0.7, 0.7, 0.75]}
        }
        wing_bsdf = {
            'type': 'diffuse',
            'reflectance': {'type': 'rgb', 'value': [0.1, 0.2, 0.4]}
        }

    transform = mi.ScalarTransform4f.translate(position)
    rotation_matrix = np.eye(4)
    rotation_matrix[:3, :3] = attitude_matrix
    transform = transform @ mi.ScalarTransform4f(rotation_matrix)

    body_scale = mi.ScalarTransform4f.scale([scale * 1.0, scale * 1.0, scale * 1.5])
    wing_scale = mi.ScalarTransform4f.scale([scale * 2.5, scale * 0.15, scale * 0.8])
    wing_offset = mi.ScalarTransform4f.translate([scale * 1.4, 0, 0])

    objects = {
        'debris_body': {
            'type': 'cube',
            'to_world': transform @ body_scale,
            'bsdf': body_bsdf
        },
        'debris_wing': {
            'type': 'cube',
            'to_world': transform @ wing_offset @ wing_scale,
            'bsdf': wing_bsdf
        }
    }

    return objects


def create_trajectory_trail(positions_km, line_radius=0.005,
                            color=(0.2, 0.6, 1.0), glow=0.5):
    """衛星が通過した軌跡を線（シリンダー列）で描画する。

    隣接する 2 点間を 1 本の cylinder で結ぶ。`SCALE_FACTOR` で km → シーン単位
    に換算してから配置する。`glow > 0` の場合は area emitter を付けて自発光させる。

    Args:
        positions_km: 過去の位置リスト (各要素は km 単位の 3D ベクトル)
        line_radius: 線の半径（レンダリング空間、地球半径=1.0）
        color: 軌跡の RGB 色
        glow: エミッター輝度倍率

    Returns:
        Mitsuba シーンオブジェクトの辞書 {'trail_0': ..., 'trail_1': ..., ...}
    """
    objects = {}
    if len(positions_km) < 2:
        return objects

    # 線分共通の BSDF / emitter (色は引数 color で固定)
    orbit_bsdf = {
        'type': 'diffuse',
        'reflectance': {'type': 'rgb', 'value': list(color)}
    }
    orbit_emitter = {
        'type': 'area',
        'radiance': {'type': 'rgb', 'value': [c * glow for c in color]}
    }

    for i in range(len(positions_km) - 1):
        p0 = np.asarray(positions_km[i]) * SCALE_FACTOR
        p1 = np.asarray(positions_km[i + 1]) * SCALE_FACTOR

        if np.linalg.norm(p1 - p0) < 1e-10:
            continue

        # 'cylinder' は p0/p1/radius の組で直接配置できる (to_world 不要)
        seg = {
            'type': 'cylinder',
            'p0': p0.tolist(),
            'p1': p1.tolist(),
            'radius': line_radius,
            'bsdf': orbit_bsdf,
        }
        if glow > 0:
            seg['emitter'] = orbit_emitter
        objects[f'trail_{i}'] = seg

    return objects


def create_relative_line(p0: np.ndarray, p1: np.ndarray, line_radius: float = 0.0005,
                         color: Tuple[float, float, float] = (1.0, 0.4, 0.2)) -> dict:
    """
    2点間の相対ベクトルを可視化する発光ライン

    cylinder のローカル軸 (Z) を p0→p1 方向に揃えるため、Z 軸との回転を
    Rodrigues の公式で計算して to_world に組み込む。
    p0/p1 はシーン単位 (地球半径=1.0)。
    """
    direction = p1 - p0
    length = np.linalg.norm(direction)
    if length < 1e-10:
        return {}

    direction_normalized = direction / length

    # Z 軸 (cylinder のデフォルト長軸) と方向ベクトルから回転軸・角度を求める
    z_axis = np.array([0, 0, 1])
    rotation_axis = np.cross(z_axis, direction_normalized)
    rotation_axis_norm = np.linalg.norm(rotation_axis)

    if rotation_axis_norm < 1e-10:
        # 方向が ±Z にほぼ平行 (cross が 0 になる) ときは個別に処理
        if direction_normalized[2] > 0:
            rotation_matrix = np.eye(3)
        else:
            rotation_matrix = np.diag([1, -1, -1])
    else:
        # Rodrigues の回転公式: R = I + sin(θ) K + (1 - cos(θ)) K^2
        rotation_axis = rotation_axis / rotation_axis_norm
        cos_angle = np.dot(z_axis, direction_normalized)
        angle = np.arccos(np.clip(cos_angle, -1, 1))
        K = np.array([
            [0, -rotation_axis[2], rotation_axis[1]],
            [rotation_axis[2], 0, -rotation_axis[0]],
            [-rotation_axis[1], rotation_axis[0], 0]
        ])
        rotation_matrix = np.eye(3) + np.sin(angle) * K + (1 - np.cos(angle)) * K @ K

    # cylinder を中点に配置し、Z 軸を p0→p1 方向に向け、長さを length に伸ばす
    center = (p0 + p1) / 2.0
    transform_matrix = np.eye(4)
    transform_matrix[:3, :3] = rotation_matrix
    transform_matrix[:3, 3] = center

    transform = mi.ScalarTransform4f(transform_matrix)
    scale = mi.ScalarTransform4f.scale([line_radius, line_radius, length / 2])

    return {
        'relative_line': {
            'type': 'cylinder',
            'to_world': transform @ scale,
            'bsdf': {
                'type': 'diffuse',
                'reflectance': {'type': 'rgb', 'value': list(color)}
            },
            'emitter': {
                'type': 'area',
                'radiance': {'type': 'rgb', 'value': [c * 2.0 for c in color]}
            }
        }
    }
