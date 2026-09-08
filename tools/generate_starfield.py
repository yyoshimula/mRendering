#!/usr/bin/env python3
"""星空環境マップ（equirectangular EXR）を HYG カタログから生成する。

HYG v4.1 (Hipparcos/Yale/Gliese 統合カタログ, ~119,000 星) の
実際の位置・等級・色指数を使い、物理的に正確な全天マップを出力する。

扱う物理:
  - equirectangular 投影（経緯度マップ、Plate Carrée、**内側から見た星図の向き**）:
        x = ((1 - RA / 2π) mod 1) * width   ; 赤経 [0, 2π) → 画像横方向（右→左、RA=0 が左端）
        y = (1/2 - Dec / π) * height        ; 赤緯 [+π/2, -π/2] → 画像縦方向（上→下）
    Mitsuba `envmap` のローカル座標は u = atan2(x, −z)/2π, v = acos(y)/π（極 = ±y、
    u は +y まわり左手回り）なので、RA を u に直接写すと鏡像（行列式 −1）になり
    回転では補正できない。上の規約で描き、optical_lighting.STARFIELD_LOCAL_TO_ECI
    （local +y→ECI +z, local −z→ECI +x）を to_world に与えると星が ECI の正しい
    方向に出る（2026-09-08 修正。旧 EXR は再生成が必要）。
  - HYG カタログの ra 列は時角（hours, 0..24）で格納されているため、
    1 h = 15° = π/12 rad で変換する。
  - 視等級 → 線形強度: I = I0 * 10^(-0.4 m)（Pogson の式）。
  - B-V 色指数 → 色温度: Ballesteros (2012) の経験式で 2 帯域測光から推定。
  - 黒体放射 → RGB: Tanner Helland 近似（線形空間）。
  - 天の川拡散光: 銀河座標 (l, b) で銀河面 b=0 を中心としたガウシアンでモデル。

出力 EXR は scene_builder.py / optical_lighting.py の
`create_starfield_envmap(hdri_path=...)` 経由でシーンに渡される。

Usage:
    python tools/generate_starfield.py                              # デフォルト
    python tools/generate_starfield.py --width 4096 --mag-limit 8.0
    python tools/generate_starfield.py --milky-way                  # 天の川拡散光を追加
    python tools/generate_starfield.py --output assets/my_stars.exr
"""

import argparse
import sys
import csv
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

# Mitsuba は EXR 書き出しにのみ使用
import mitsuba as mi
mi.set_variant('scalar_rgb')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo ルート（optical_lighting）
from optical_lighting import blackbody_to_rgb  # noqa: E402
from asset_bootstrap import write_starfield_sidecar, STARFIELD_CONVENTION  # noqa: E402

CATALOG_PATH = Path(__file__).parent / 'hyg_catalog.csv'


# ---------------------------------------------------------------------------
# B-V 色指数 → 色温度 → RGB
# ---------------------------------------------------------------------------

def bv_to_temperature(bv: float) -> float:
    """B-V 色指数から色温度 [K] を Ballesteros (2012) の式で推定する。

    B-V は B バンド（青）と V バンド（緑〜黄）の等級差で、星のスペクトル型と
    強く相関する。負（青く高温）から正（赤く低温）へ。
        T = 4600 * [ 1/(0.92*BV + 1.7) + 1/(0.92*BV + 0.62) ]
    実用範囲は -0.4 ≤ BV ≤ 2.0（O型〜M型程度）。
    """
    # T = 4600 * (1/(0.92*BV + 1.7) + 1/(0.92*BV + 0.62))
    bv = np.clip(bv, -0.4, 2.0)
    t = 4600.0 * (1.0 / (0.92 * bv + 1.7) + 1.0 / (0.92 * bv + 0.62))
    return float(np.clip(t, 2000, 40000))


def blackbody_rgb(temperature: float) -> np.ndarray:
    """黒体放射色温度 [K] → 線形 RGB (0-1, 最大成分 1)。

    optical_lighting.blackbody_to_rgb（Planck × CIE 1931 → 線形 sRGB）を共用する。
    旧実装は Tanner Helland 式（sRGB 表示値のフィット）を「線形」と称していた。
    """
    return np.asarray(blackbody_to_rgb(float(temperature)), dtype=float)


# ---------------------------------------------------------------------------
# カタログ読み込み
# ---------------------------------------------------------------------------

def load_catalog(
    catalog_path: Path,
    mag_limit: float = 7.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """HYG カタログから (ra_rad, dec_rad, mag, temperature) を返す。

    HYG CSV のスキーマ:
      - ra: 赤経（時角単位 hours, 0..24）
      - dec: 赤緯（度, -90..+90）
      - mag: 見かけの等級
      - ci: B-V 色指数（任意）
      - dist: 距離（パーセク）。0 は太陽自身を意味するため除外する

    肉眼限界は概ね 6.5 等級。デフォルト 7.5 は望遠鏡なしの限界より少し暗いところ
    まで含めて、レンダリング上の "天の川感" を出す目的。

    Returns:
        ra_rad:  赤経 [rad], shape (N,)
        dec_rad: 赤緯 [rad], shape (N,)
        mag:     見かけの等級, shape (N,)
        temp:    色温度 [K], shape (N,)
    """
    ra_list, dec_list, mag_list, temp_list = [], [], [], []

    with open(catalog_path, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ra = float(row['ra'])
                dec = float(row['dec'])
                mag = float(row['mag'])
            except (ValueError, KeyError):
                continue

            if mag > mag_limit:
                continue
            # 太陽 (dist=0) をスキップ
            try:
                dist = float(row['dist'])
                if dist == 0.0:
                    continue
            except (ValueError, KeyError):
                pass

            # B-V 色指数 → 色温度
            ci_str = row.get('ci', '')
            if ci_str:
                try:
                    temp = bv_to_temperature(float(ci_str))
                except ValueError:
                    temp = 5778.0  # デフォルト: 太陽型
            else:
                temp = 5778.0

            # HYG の ra は時角 (hours): 1 h = 15° → rad に変換
            ra_list.append(np.radians(ra * 15.0))
            dec_list.append(np.radians(dec))
            mag_list.append(mag)
            temp_list.append(temp)

    print(f"カタログ読み込み: {len(ra_list)} 星 (等級 ≤ {mag_limit})")
    return (
        np.array(ra_list, dtype=np.float64),
        np.array(dec_list, dtype=np.float64),
        np.array(mag_list, dtype=np.float64),
        np.array(temp_list, dtype=np.float64),
    )


# ---------------------------------------------------------------------------
# 天の川帯（拡散光）
# ---------------------------------------------------------------------------

def make_milky_way_density(height: int, width: int) -> np.ndarray:
    """天の川の銀河面に沿った密度マップ (0-1) を返す。

    赤道座標 (α, δ) を銀河座標 (l, b) に変換し、銀河緯度 b ≈ 0 を中心とした
    ガウシアン（FWHM ~ 2.355σ ≈ 14°）で拡散光を近似する。
    変換式（J2000 の銀河北極を基準とする標準球面三角法）:
        sin b = sin δ_NGP sin δ + cos δ_NGP cos δ cos(α - α_NGP)
    銀河中心 (l ≈ 0°, いて座方向) を中心とした追加のガウシアンで明るさを
    ブーストし、見た目の偏在を再現。
    """
    # equirectangular グリッドを構築（星の px/py と同じ規約: pixel_of_ra_dec）
    # dec は上端 +π/2 → 下端 -π/2、ra は左 0 → 右へ **減少**（内側から見た星図）
    dec = (0.5 - (np.arange(height) + 0.5) / height) * np.pi
    ra = (1.0 - (np.arange(width) + 0.5) / width) * 2 * np.pi
    ra_grid, dec_grid = np.meshgrid(ra, dec)

    # 赤道座標 → 銀河緯度の近似変換
    # 銀河北極 NGP: RA=192.86°, Dec=27.13° (J2000)
    ra_ngp = np.radians(192.86)
    dec_ngp = np.radians(27.13)
    l_ncp = np.radians(122.93)  # 銀経の北天極オフセット（昇交点経度）

    # 銀河緯度 b
    sin_b = (np.sin(dec_ngp) * np.sin(dec_grid)
             + np.cos(dec_ngp) * np.cos(dec_grid) * np.cos(ra_grid - ra_ngp))
    gal_b = np.arcsin(np.clip(sin_b, -1, 1))

    # 銀経 l の計算（銀河中心の位置特定に使用）
    sin_l = np.cos(dec_grid) * np.sin(ra_grid - ra_ngp) / np.cos(gal_b)
    cos_l = ((np.sin(dec_grid) - np.sin(dec_ngp) * sin_b)
             / (np.cos(dec_ngp) * np.cos(gal_b)))
    gal_l = l_ncp - np.arctan2(sin_l, cos_l)

    # 銀河面の帯: |b| < ~6° で集中（ガウシアン）
    sigma = np.radians(6)  # 帯の幅
    density = np.exp(-0.5 * (gal_b / sigma) ** 2)

    # 銀河中心方向 (l=0、いて座方向) をブースト
    center_boost = np.exp(-0.5 * (gal_l / np.radians(40)) ** 2)
    density *= (1.0 + 0.8 * center_boost)

    return (density / density.max()).astype(np.float32)


# ---------------------------------------------------------------------------
# メイン生成
# ---------------------------------------------------------------------------

def pixel_of_ra_dec(ra_rad: np.ndarray, dec_rad: np.ndarray,
                    width: int, height: int) -> Tuple[np.ndarray, np.ndarray]:
    """赤経・赤緯 [rad] → 画素 (px, py)。星図規約: u = (1 − RA/2π) mod 1, v = 1/2 − Dec/π。

    Mitsuba envmap のローカル座標で u = atan2(x, −z)/2π なので、この画像を
    STARFIELD_LOCAL_TO_ECI（local −z = RA 0 = ECI +x, local +y = ECI +z）で回すと
    方向 (RA, Dec) がこの画素を指す。
    """
    ra = np.asarray(ra_rad, dtype=float)
    dec = np.asarray(dec_rad, dtype=float)
    u = (1.0 - ra / (2 * np.pi)) % 1.0
    px = np.floor(u * width).astype(int) % width
    py = np.clip(np.floor((0.5 - dec / np.pi) * height).astype(int), 0, height - 1)
    return px, py


def magnitude_to_intensity(mag: np.ndarray, mag_zero_intensity: float = 0.8) -> np.ndarray:
    """見かけの等級を相対輝度に変換する（ベクトル対応）。

    Pogson の式: I = I0 * 10^(-0.4 m)
    mag_zero_intensity は 0 等星のピクセル輝度に相当する基準値で、レンダリング
    上の見栄えに合わせて経験的に選ぶ。
    """
    return mag_zero_intensity * 10.0 ** (-0.4 * mag)


def generate_starfield(
    catalog_path: Path = CATALOG_PATH,
    width: int = 4096,
    height: Optional[int] = None,
    mag_limit: float = 7.5,
    milky_way: bool = False,
    milky_way_brightness: float = 0.002,
) -> np.ndarray:
    """HYG カタログから equirectangular 星空画像を生成する。

    出力形式は Mitsuba の `envmap` プラグインが期待する経緯度マップ
    （アスペクト比 2:1）。各星は 1 ピクセルに加算され、明るい星には
    十字＋斜めパターンの光のにじみ（ブルーム）を追加する。
    """
    if height is None:
        # equirectangular 投影の標準アスペクト比
        height = width // 2

    ra_rad, dec_rad, mag, temp = load_catalog(catalog_path, mag_limit)

    image = np.zeros((height, width, 3), dtype=np.float32)

    # --- 天の川の拡散光 ---
    # 銀河面に沿った 4500 K（やや黄色）の連続光をベースに重ねる
    if milky_way:
        mw_density = make_milky_way_density(height, width)
        mw_color = blackbody_rgb(4500)
        image += mw_density[:, :, None] * mw_color[None, None, :] * milky_way_brightness

    # --- equirectangular 投影（星図規約、pixel_of_ra_dec 参照）---
    px, py = pixel_of_ra_dec(ra_rad, dec_rad, width, height)

    intensities = magnitude_to_intensity(mag)

    # 色温度テーブル（ユニーク温度でキャッシュ、blackbody_rgb のコスト削減）
    color_cache = {}

    for i in range(len(mag)):
        t = int(temp[i] / 10) * 10  # 10K 刻みで丸め
        if t not in color_cache:
            color_cache[t] = blackbody_rgb(float(t))
        # 中心ピクセルへ加算（複数星が同じピクセルに落ちれば自然に重なる）
        color = color_cache[t] * intensities[i]

        x, y = px[i], py[i]
        image[y, x] += color.astype(np.float32)

        # 明るい星（等級 < 2）に十字パターンのブルームを追加
        # （実カメラの光条・回折スパイクの安価な近似）
        if mag[i] < 2.0:
            bloom = intensities[i] * 0.15
            bloom_color = (color_cache[t] * bloom).astype(np.float32)
            for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                ny, nx = y + dy, x + dx
                if 0 <= ny < height and 0 <= nx < width:
                    image[ny, nx] += bloom_color
            # 1 等星以上はさらに外側（マンハッタン距離 2）にもブルーム
            if mag[i] < 1.0:
                bloom2 = intensities[i] * 0.05
                bloom2_color = (color_cache[t] * bloom2).astype(np.float32)
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        if abs(dy) + abs(dx) == 2:
                            ny, nx = y + dy, x + dx
                            if 0 <= ny < height and 0 <= nx < width:
                                image[ny, nx] += bloom2_color

    n_bright = np.sum(mag < 2.0)
    print(f"配置完了: {len(mag)} 星 (うち 2 等星以上: {n_bright})")
    return image


def save_exr(image: np.ndarray, path: str) -> None:
    """NumPy float32 配列を Mitsuba 経由で EXR に保存する。

    EXR は HDR 線形空間。0..1 を超える値もそのまま保存され、Mitsuba の
    `envmap` プラグインで物理的に正しい強度として扱える。
    """
    h, w, _ = image.shape
    bitmap = mi.Bitmap(image, mi.Bitmap.PixelFormat.RGB)
    bitmap.write(path)
    print(f"保存: {path}  ({w}x{h}, {image.nbytes / 1024 / 1024:.1f} MB)")
    # 投影規約のサイドカー（asset_bootstrap が起動時に照合し、旧規約 EXR を作り直す）
    write_starfield_sidecar(Path(path), width=int(image.shape[1]), height=int(image.shape[0]),
                            generator='tools/generate_starfield.py')


def main():
    parser = argparse.ArgumentParser(description='HYG カタログベース星空 EXR を生成')
    parser.add_argument('--catalog', type=str, default=str(CATALOG_PATH),
                        help='HYG カタログ CSV のパス')
    parser.add_argument('--width', type=int, default=4096, help='画像幅 (高さは幅/2)')
    parser.add_argument('--mag-limit', type=float, default=7.5,
                        help='含める最大等級 (デフォルト: 7.5, 肉眼限界≈6.5)')
    parser.add_argument('--milky-way', action='store_true', help='天の川拡散光を追加')
    parser.add_argument('--milky-way-brightness', type=float, default=0.002,
                        help='天の川の明るさ')
    parser.add_argument('--output', type=str, default='assets/starfield.exr',
                        help='出力パス')
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    image = generate_starfield(
        catalog_path=Path(args.catalog),
        width=args.width,
        mag_limit=args.mag_limit,
        milky_way=args.milky_way,
        milky_way_brightness=args.milky_way_brightness,
    )
    save_exr(image, str(output))


if __name__ == '__main__':
    main()
