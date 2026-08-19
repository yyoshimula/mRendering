"""`mrender lightcurve` 動詞: 主物体の地上観測ライトカーブを CSV 出力する。

地上観測者（緯度経度高度）から見たときの観測物体の見かけ輝度を時系列で求め、
`light_curve.csv` として保存する。実体は `satellite_orbit.render_light_curve`
で、本ファイルは引数解決と RunDir 管理だけを行う。
フレーム連番 PNG は通常出さないので `args.no_frames=True` を立てる。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Dict

from runs import RunDir, create_run_dir
from verbs._common import frame_range, resolve_args, resolve_run_inputs


def run(args: argparse.Namespace, run_dir: RunDir | None = None) -> RunDir:
    """主物体のライトカーブを計算し、`light_curve.csv` を保存する。

    Args:
        args: 解決済み引数。`observer_lat/lon/alt`, `light_curve_fov`,
            `light_curve_samples` などライトカーブ固有のパラメータを参照。
        run_dir: 既存ランへ追記する場合に渡す。`None` なら新規 RunDir を作り、
            ライトカーブ専用ランとして frames 出力をスキップする。

    Returns:
        使用した `RunDir`。
    """
    from satellite_orbit import print_run_summary, render_light_curve

    # satellite_orbit 側はこのフラグでライトカーブ用カメラ生成を分岐する。
    args.light_curve = True

    config, objects, earth_day_texture = resolve_run_inputs(args)
    start_frame, end_frame = frame_range(args)
    args.start_frame = start_frame
    args.end_frame = end_frame

    primary_name = getattr(args, 'primary_name', None) or objects[0].name
    owns_run = run_dir is None
    if run_dir is None:
        # ライトカーブ単独ランでは PNG を出さない（CSV だけが成果物）。
        args.no_frames = True
        run_dir = create_run_dir('lightcurve', args, name_hint=primary_name)
    args.output_dir = str(run_dir.path)

    if owns_run:
        print_run_summary(args, objects, config, run_dir.path, earth_day_texture, start_frame, end_frame)

    status = 'ok'
    extra: Dict[str, object] = {}
    try:
        light_curve_path = render_light_curve(
            args.frames,
            start_frame,
            end_frame,
            objects[0],
            config,
            run_dir.path,
            args.observer_lat,    # 観測者緯度 [deg]
            args.observer_lon,    # 観測者経度 [deg]
            args.observer_alt,    # 観測者標高 [km]
            args.light_curve_fov,        # 仮想望遠鏡の画角 [deg]
            args.light_curve_samples,    # 1 サンプルあたりのレイ数
        )
        extra['light_curve_csv'] = str(light_curve_path)
        extra['frame_count'] = end_frame - start_frame
        print(f'ライトカーブ出力: {light_curve_path}')
    except Exception as exc:
        status = f'error: {exc!r}'
        raise
    finally:
        if owns_run:
            run_dir.write_manifest(args, finished_at=datetime.now(), status=status, extra=extra)
        else:
            run_dir.manifest_extra.setdefault('lightcurve', {}).update(extra)

    if owns_run:
        print(f"\n{'=' * 60}")
        print(f'完了: {end_frame - start_frame} サンプル.')
        print(f'ランディレクトリ: {run_dir.path}')
        print(f"{'=' * 60}")
    return run_dir


def main(argv=None) -> None:
    """CLI エントリポイント。`mrender lightcurve ...` から呼ばれる。"""
    args = resolve_args(argv)
    run(args)


if __name__ == '__main__':
    main()
