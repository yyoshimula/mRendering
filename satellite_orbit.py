#!/usr/bin/env python3
"""
地球周りを運動する人工衛星のMitsuba 3レンダリングスクリプト

使用方法:
    source venv/bin/activate
    python satellite_orbit.py --frames 60 --orbit-radius 10
"""

import numpy as np
import argparse
from pathlib import Path
import mitsuba as mi

# Mitsuba 3のバリアントを設定（scalar_rgb: シンプルなCPUレンダリング）
mi.set_variant('scalar_rgb')


def create_earth(radius=1.0, texture_path=None):
    """
    地球を表す球体を作成

    Args:
        radius: 地球の半径（基準値: 1.0）
        texture_path: テクスチャ画像へのパス（オプション）

    Returns:
        Mitsubaシェイプオブジェクト
    """
    bsdf = {
        'type': 'diffuse'
    }

    if texture_path:
        bsdf['reflectance'] = {
            'type': 'bitmap',
            'filename': str(texture_path)
        }
    else:
        bsdf['reflectance'] = {
            'type': 'rgb',
            'value': [0.2, 0.4, 0.8]  # 青い地球
        }

    return {
        'type': 'sphere',
        'center': [0, 0, 0],
        'radius': radius,
        'bsdf': bsdf
    }


def create_satellite(position, size=0.15):
    """
    人工衛星を作成（簡単な箱型）

    Args:
        position: 衛星の位置 [x, y, z]
        size: 衛星のサイズ

    Returns:
        Mitsubaシェイプオブジェクト
    """
    # 衛星本体（箱型）
    satellite_body = {
        'type': 'cube',
        'to_world': mi.ScalarTransform4f
            .translate(position)
            .scale([size, size * 0.5, size * 0.8]),
        'bsdf': {
            'type': 'conductor',  # 金属質な表面
            'material': 'Al',     # アルミニウム
        }
    }

    return satellite_body


def create_solar_panels(position, size=0.15):
    """
    人工衛星のソーラーパネルを作成

    Args:
        position: 衛星の位置 [x, y, z]
        size: 基準サイズ

    Returns:
        ソーラーパネルのリスト
    """
    panels = []
    panel_width = size * 2.0
    panel_height = size * 0.8
    panel_thickness = size * 0.05

    # 左側のソーラーパネル
    left_panel = {
        'type': 'cube',
        'to_world': mi.ScalarTransform4f
            .translate([position[0] - size * 1.5, position[1], position[2]])
            .scale([panel_width, panel_thickness, panel_height]),
        'bsdf': {
            'type': 'diffuse',
            'reflectance': {
                'type': 'rgb',
                'value': [0.1, 0.1, 0.3]  # 濃紺のソーラーパネル
            }
        }
    }

    # 右側のソーラーパネル
    right_panel = {
        'type': 'cube',
        'to_world': mi.ScalarTransform4f
            .translate([position[0] + size * 1.5, position[1], position[2]])
            .scale([panel_width, panel_thickness, panel_height]),
        'bsdf': {
            'type': 'diffuse',
            'reflectance': {
                'type': 'rgb',
                'value': [0.1, 0.1, 0.3]
            }
        }
    }

    panels.append(left_panel)
    panels.append(right_panel)

    return panels


def calculate_satellite_position(frame, total_frames, orbit_radius, orbit_inclination=0):
    """
    衛星の軌道上の位置を計算

    Args:
        frame: 現在のフレーム番号
        total_frames: 総フレーム数
        orbit_radius: 軌道半径
        orbit_inclination: 軌道傾斜角（度）

    Returns:
        位置ベクトル [x, y, z]
    """
    # 軌道上の角度（0から2πまで）
    theta = 2 * np.pi * frame / total_frames

    # 軌道傾斜角をラジアンに変換
    inclination_rad = np.radians(orbit_inclination)

    # 円軌道上の位置を計算
    x = orbit_radius * np.cos(theta)
    y = orbit_radius * np.sin(theta) * np.cos(inclination_rad)
    z = orbit_radius * np.sin(theta) * np.sin(inclination_rad)

    return [x, y, z]


def create_scene(satellite_position, camera_position=None, camera_target=None, earth_texture=None, width=1920, height=1080):
    """
    Mitsubaシーンを作成

    Args:
        satellite_position: 衛星の位置
        camera_position: カメラの位置（Noneの場合は自動計算）
        camera_target: カメラの注視点（Noneの場合は地球中心）
        earth_texture: 地球のテクスチャパス（Noneの場合は単色）
        width: 画像の幅
        height: 画像の高さ

    Returns:
        Mitsubaシーン辞書
    """
    if camera_position is None:
        # カメラ位置を自動計算（地球と衛星が両方見える位置）
        # 軌道半径の1.5倍の距離から見る
        orbit_radius = np.sqrt(satellite_position[0]**2 +
                              satellite_position[1]**2 +
                              satellite_position[2]**2)
        camera_distance = orbit_radius * 1.5

        # カメラを斜め上から配置
        camera_position = [
            satellite_position[0] * 0.3,
            satellite_position[1] * 0.3 - camera_distance * 0.8,
            camera_distance * 0.6
        ]

    if camera_target is None:
        # 地球と衛星の中間点を見る
        camera_target = [
            satellite_position[0] * 0.3,
            satellite_position[1] * 0.3,
            satellite_position[2] * 0.3
        ]

    scene_dict = {
        'type': 'scene',

        # 積分器（レンダリング手法）
        'integrator': {
            'type': 'path',  # パストレーシング
            'max_depth': 8,
        },

        # カメラ
        'camera': {
            'type': 'perspective',
            'fov': 45,
            'to_world': mi.ScalarTransform4f.look_at(
                origin=camera_position,
                target=camera_target,
                up=[0, 0, 1]
            ),
            'film': {
                'type': 'hdrfilm',
                'width': width,
                'height': height,
                'rfilter': {
                    'type': 'gaussian',
                },
            },
            'sampler': {
                'type': 'independent',
                'sample_count': 64,  # サンプル数（高いほど高品質だが遅い）
            },
        },

        # 太陽光（指向性光源）
        'sun': {
            'type': 'directional',
            'direction': [1, -0.5, -0.3],
            'irradiance': {
                'type': 'rgb',
                'value': [3.0, 3.0, 2.8],  # 太陽光の色
            }
        },

        # 環境光（宇宙の背景）
        'envmap': {
            'type': 'constant',
            'radiance': {
                'type': 'rgb',
                'value': [0.01, 0.01, 0.02],  # 暗い宇宙
            }
        },

        # 地球
        'earth': create_earth(radius=1.0, texture_path=earth_texture),

        # 人工衛星本体
        'satellite': create_satellite(satellite_position, size=0.15),
    }

    # ソーラーパネルを追加
    solar_panels = create_solar_panels(satellite_position, size=0.15)
    for i, panel in enumerate(solar_panels):
        scene_dict[f'solar_panel_{i}'] = panel

    return scene_dict


def render_frame(frame, total_frames, orbit_radius, orbit_inclination, output_dir, earth_texture=None, width=1920, height=1080):
    """
    1フレームをレンダリング

    Args:
        frame: フレーム番号
        total_frames: 総フレーム数
        orbit_radius: 軌道半径
        orbit_inclination: 軌道傾斜角
        output_dir: 出力ディレクトリ
        earth_texture: 地球のテクスチャパス
        width: 画像の幅
        height: 画像の高さ
    """
    # 衛星の位置を計算
    satellite_pos = calculate_satellite_position(
        frame, total_frames, orbit_radius, orbit_inclination
    )

    # シーンを作成
    scene_dict = create_scene(satellite_pos, earth_texture=earth_texture, width=width, height=height)
    scene = mi.load_dict(scene_dict)

    # レンダリング
    print(f"レンダリング中: フレーム {frame+1}/{total_frames} (衛星位置: {satellite_pos})")
    image = mi.render(scene)

    # 画像を保存
    output_path = output_dir / f"frame_{frame:04d}.png"
    mi.util.write_bitmap(str(output_path), image)
    print(f"保存完了: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Mitsuba 3で地球周りを運動する人工衛星をレンダリング'
    )
    parser.add_argument(
        '--frames',
        type=int,
        default=60,
        help='レンダリングするフレーム数（デフォルト: 60）'
    )
    parser.add_argument(
        '--orbit-radius',
        type=float,
        default=10.0,
        help='軌道半径（地球半径の倍数、デフォルト: 10.0）'
    )
    parser.add_argument(
        '--orbit-inclination',
        type=float,
        default=23.5,
        help='軌道傾斜角（度、デフォルト: 23.5）'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        default='output',
        help='出力ディレクトリ（デフォルト: output）'
    )
    parser.add_argument(
        '--start-frame',
        type=int,
        default=0,
        help='開始フレーム（デフォルト: 0）'
    )
    parser.add_argument(
        '--end-frame',
        type=int,
        default=None,
        help='終了フレーム（デフォルト: None = 最後まで）'
    )
    parser.add_argument(
        '--earth-texture',
        type=str,
        default=None,
        help='地球のテクスチャ画像パス（オプション）'
    )
    parser.add_argument(
        '--width',
        type=int,
        default=1920,
        help='画像の幅（デフォルト: 1920）'
    )
    parser.add_argument(
        '--height',
        type=int,
        default=1080,
        help='画像の高さ（デフォルト: 1080）'
    )

    args = parser.parse_args()

    # 出力ディレクトリを作成
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # フレーム範囲を設定
    start_frame = args.start_frame
    end_frame = args.end_frame if args.end_frame is not None else args.frames

    print(f"=== 人工衛星軌道レンダリング ===")
    print(f"総フレーム数: {args.frames}")
    print(f"レンダリング範囲: {start_frame} - {end_frame}")
    print(f"軌道半径: {args.orbit_radius} × 地球半径")
    print(f"軌道傾斜角: {args.orbit_inclination}度")
    print(f"解像度: {args.width}x{args.height}")
    print(f"テクスチャ: {args.earth_texture if args.earth_texture else 'なし（デフォルト色）'}")
    print(f"出力ディレクトリ: {output_dir}")
    print(f"================================\n")

    # 各フレームをレンダリング
    for frame in range(start_frame, end_frame):
        render_frame(
            frame,
            args.frames,
            args.orbit_radius,
            args.orbit_inclination,
            output_dir,
            earth_texture=args.earth_texture,
            width=args.width,
            height=args.height
        )

    print(f"\n完了！{end_frame - start_frame}フレームをレンダリングしました。")
    print(f"出力: {output_dir}/frame_*.png")


if __name__ == '__main__':
    main()
