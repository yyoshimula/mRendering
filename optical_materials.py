#!/usr/bin/env python3
"""
光学材質モジュール

宇宙機で実際に使われる材料の光学特性（複素屈折率・粗さ・色）を、
Mitsuba 3 の BSDF (Bidirectional Scattering Distribution Function) 辞書として返す。

扱う BSDF タイプとその物理的意味:
  - conductor       : 完全鏡面の金属反射。複素屈折率 ñ = n + ik （k は消衰係数で
                      強い吸収を表す）から Fresnel 方程式で反射率を計算。
  - roughconductor  : マイクロファセット理論（GGX 等）に基づく粗面金属。`alpha` は
                      表面の RMS 粗さに対応（大きいほど鏡面が広がる）。
  - dielectric      : 誘電体の鏡面反射＋透過。`int_ior`/`ext_ior` は内部/外部の
                      実屈折率比で Snell 則と Fresnel 反射率を与える。
  - roughdielectric : マイクロファセット粗面誘電体（曇りガラス等）。
  - plastic / roughplastic : 拡散層 + 鏡面層の合成。塗装面・コーティング面の近似。
  - diffuse         : Lambert 拡散（cosθ 則）。一様反射する理想拡散面。

宇宙機材料の代表例:
  - ブラシ仕上げアルミ : 異方性 roughconductor。衛星本体の構造材として最一般。
  - 金メッキ          : conductor (Au)。アンテナ・MLI・サーマル制御で多用。
  - 太陽電池          : 大半の光を吸収するため非常に暗い拡散反射体。微青色。
  - MLI (カプトン)    : 金薄膜のキラキラした鏡面 + 拡散の混合 (blendbsdf)。
  - 白色塗装/黒色塗装 : サーマル制御。α (吸収率) と ε (放射率) の比で熱平衡を調整。

使用側: scene_builder.py / satellite_orbit.py 等で衛星パーツに割り当てる。
"""

from typing import Dict, Any, Optional
import numpy as np


class SpaceMaterial:
    """宇宙材料の光学特性データベース

    Mitsuba は文字列指定 (`'material': 'Al'` 等) で内部の波長依存複素屈折率
    テーブルを参照できるが、ここでは波長平均した代表値を保持して将来の
    手動 conductor 構成（`'eta': ..., 'k': ...` 直接指定）にも対応できるようにする。
    """

    # 導体材料の複素屈折率データ（可視光域の近似値）
    # 複素屈折率 ñ = n + ik:
    #   n: 通常の屈折率（位相速度 c/n）
    #   k: 消衰係数（吸収を表す。大きいほど強く吸収＝高反射の金属性）
    # 形式: (屈折率 n, 消衰係数 k)
    CONDUCTOR_IOR = {
        'aluminum': {'eta': 1.5, 'k': 7.5},      # アルミニウム（広帯域で高反射）
        'gold': {'eta': 0.47, 'k': 2.8},         # 金（n<1 の異常分散、青を吸収し黄金色に）
        'silver': {'eta': 0.18, 'k': 3.6},       # 銀（可視全域で最高反射率）
        'copper': {'eta': 0.27, 'k': 3.6},       # 銅（緑〜青を吸収し赤色に）
        'titanium': {'eta': 2.7, 'k': 3.5},      # チタン（やや暗いグレー）
    }

    # 誘電体材料の屈折率（k≈0 で吸収なし、Snell 則と Fresnel 反射のみ）
    DIELECTRIC_IOR = {
        'vacuum': 1.0,
        'air': 1.000293,
        'water': 1.333,
        'glass': 1.5,
        'sapphire': 1.77,                        # サファイアガラス（耐放射線窓材）
        'quartz': 1.46,                          # 石英ガラス（光学窓・ソーラーセルカバー）
        'kapton': 1.7,                           # カプトン（断熱材ベースフィルム）
        'teflon': 1.35,                          # テフロン（PTFE）
    }

    # 材料の粗さ（マイクロファセットモデルの alpha 値、~RMS 傾き）
    # GGX/Beckmann 分布で正規化済み: 0=完全鏡面, ~1 で広い拡散反射に近づく
    ROUGHNESS = {
        'mirror': 0.0,
        'polished': 0.05,
        'brushed': 0.2,
        'sandblasted': 0.5,
        'rough': 0.8,
    }


def create_aluminum_bsdf(roughness: float = 0.1, anisotropic: bool = False) -> Dict[str, Any]:
    """
    アルミニウムのBSDFを作成（衛星本体に使用）

    実機の宇宙機構造材で最も頻出。粗さに応じて conductor / roughconductor を切替。
    ブラシ仕上げ（ヘアライン加工）は研磨方向に沿った異方性反射が出るため、
    `alpha_u` と `alpha_v` を別々に与える。

    Args:
        roughness: 表面粗さ (0.0-1.0)
        anisotropic: 異方性反射を有効化（ブラシ仕上げなど）

    Returns:
        Mitsuba BSDF辞書
    """
    if roughness < 0.01:
        # 滑らかな表面: 完全鏡面反射
        return {
            'type': 'conductor',
            'material': 'Al',  # Mitsuba 内部の Al 複素屈折率テーブルを使用
        }
    else:
        # 粗い表面: 粗面導体（マイクロファセット）
        bsdf = {
            'type': 'roughconductor',
            'material': 'Al',
        }

        if anisotropic:
            # 異方性（ブラシ仕上げ）: u 方向に粗く、v 方向は滑らか
            bsdf['alpha_u'] = roughness
            bsdf['alpha_v'] = roughness * 0.3
        else:
            # 等方性: 単一 alpha でファセット分布幅を指定
            bsdf['alpha'] = roughness

        return bsdf


def create_gold_bsdf(roughness: float = 0.05) -> Dict[str, Any]:
    """
    金のBSDFを作成（アンテナ、多層断熱材MLIに使用）

    金は青を強く吸収し、長波長を高効率で反射するため特徴的な金色。
    赤外でも反射率が高く、サーマル制御や RF アンテナに広く使われる。

    Args:
        roughness: 表面粗さ (0.0-1.0)

    Returns:
        Mitsuba BSDF辞書
    """
    if roughness < 0.01:
        return {
            'type': 'conductor',
            'material': 'Au',  # 内部 Au 複素屈折率テーブル
        }
    else:
        return {
            'type': 'roughconductor',
            'material': 'Au',
            'alpha': max(roughness, 0.001),  # 最小値を設定（数値安定性のため 0 を避ける）
        }


def create_solar_panel_bsdf(reflectance: float = 0.05) -> Dict[str, Any]:
    """
    太陽電池パネルのBSDFを作成

    Si 系セルは可視光のうち発電に使う波長を強く吸収するため、外見は非常に暗い。
    AR コートにより青寄りに見えることが多く、ここでは R, G を抑え B を残した
    濃紺〜暗青に設定する。簡易のため拡散反射のみで近似（厳密にはセルガラスの
    Fresnel 反射成分も加わる）。

    Args:
        reflectance: 反射率 (0.0-1.0, 通常は0.05程度)

    Returns:
        Mitsuba BSDF辞書
    """
    return {
        'type': 'diffuse',
        'reflectance': {
            'type': 'rgb',
            'value': [reflectance * 0.2, reflectance * 0.2, reflectance]  # 青みがかった暗色
        }
    }


def create_white_paint_bsdf(roughness: float = 0.8) -> Dict[str, Any]:
    """
    白色塗装のBSDFを作成（熱制御用）

    宇宙機の熱制御では「太陽吸収率 α が低く、赤外放射率 ε が高い」材料が
    必要で、白色塗装（例: AZ-93）が代表的。レンダリング上は高反射拡散層 +
    弱い鏡面層を持つ roughplastic で近似する。

    Args:
        roughness: 表面粗さ (0.0-1.0、鏡面層のマイクロファセット幅)

    Returns:
        Mitsuba BSDF辞書
    """
    return {
        'type': 'roughplastic',
        'diffuse_reflectance': {
            'type': 'rgb',
            'value': [0.9, 0.9, 0.9]  # 高反射率
        },
        'alpha': roughness,
    }


def create_black_paint_bsdf(roughness: float = 0.8) -> Dict[str, Any]:
    """
    黒色塗装のBSDFを作成（熱制御用ラジエーター）

    黒色塗装は α・ε ともに高く、赤外放射で熱を捨てる用途（ラジエーター内側、
    迷光抑制塗装）に使う。可視ではほぼすべて吸収するため低反射拡散で近似。

    Args:
        roughness: 表面粗さ (0.0-1.0)（現状の diffuse 実装では未使用）

    Returns:
        Mitsuba BSDF辞書
    """
    return {
        'type': 'diffuse',
        'reflectance': {
            'type': 'rgb',
            'value': [0.05, 0.05, 0.05]  # 低反射率
        }
    }


def create_kapton_mli_bsdf() -> Dict[str, Any]:
    """
    カプトンMLI（多層断熱材）のBSDFを作成

    MLI（Multi-Layer Insulation）は金属蒸着したカプトンフィルムを多層化した
    断熱材。表面はキラキラした金色の鏡面反射と、シワによる拡散反射が混在する。
    Mitsuba の `blendbsdf` で 70% conductor + 30% diffuse の重ね合わせとして表現。
      - weight: bsdf_0 と bsdf_1 の混合比（1.0 = bsdf_1 のみ）
      - 注意: weight の意味は Mitsuba のバージョンに依存

    Returns:
        Mitsuba BSDF辞書
    """
    return {
        'type': 'blendbsdf',
        'weight': 0.7,  # 70%鏡面反射
        'bsdf_0': {
            'type': 'conductor',
            'material': 'Au',
        },
        'bsdf_1': {
            'type': 'diffuse',
            'reflectance': {
                'type': 'rgb',
                'value': [0.8, 0.6, 0.2]  # 金色
            }
        }
    }


def create_glass_bsdf(ior: float = 1.5, roughness: float = 0.0) -> Dict[str, Any]:
    """
    ガラス（カメラレンズ、窓）のBSDFを作成

    誘電体は Snell 則による屈折と Fresnel 反射の両方を持つ。Mitsuba では:
      - int_ior: 媒質内側の屈折率（ガラス側）
      - ext_ior: 媒質外側の屈折率（宇宙空間 ≈ 1.0）
      - alpha: 粗面誘電体のマイクロファセット幅（曇りガラス）

    Args:
        ior: 屈折率（典型: ガラス 1.5、サファイア 1.77）
        roughness: 表面粗さ (0.0-1.0)

    Returns:
        Mitsuba BSDF辞書
    """
    if roughness < 0.01:
        # 滑らかなガラス: Fresnel 反射 + 屈折透過
        return {
            'type': 'dielectric',
            'int_ior': ior,
            'ext_ior': 1.0,  # 真空
        }
    else:
        # 粗いガラス: マイクロファセットによる散乱が加わる
        return {
            'type': 'roughdielectric',
            'int_ior': ior,
            'ext_ior': 1.0,
            'alpha': roughness,
        }


def create_carbon_composite_bsdf(roughness: float = 0.3) -> Dict[str, Any]:
    """
    カーボンコンポジット（CFRP）のBSDFを作成

    炭素繊維強化プラスチック。樹脂層（誘電体）の Fresnel 鏡面 +
    繊維による暗い拡散反射を roughplastic で近似。
    `int_ior=1.6` は典型的なエポキシ樹脂の屈折率。

    Args:
        roughness: 表面粗さ (0.0-1.0)

    Returns:
        Mitsuba BSDF辞書
    """
    return {
        'type': 'roughplastic',
        'diffuse_reflectance': {
            'type': 'rgb',
            'value': [0.1, 0.1, 0.12]  # ダークグレー
        },
        'alpha': roughness,
        'int_ior': 1.6,
    }


def create_beta_cloth_bsdf() -> Dict[str, Any]:
    """
    ベータクロス（耐熱繊維）のBSDFを作成

    ガラス繊維にテフロンコートした白色の耐熱布。EVA グローブや MLI 最外層に
    使われる。ほぼ完全拡散の白として近似。

    Returns:
        Mitsuba BSDF辞書
    """
    return {
        'type': 'diffuse',
        'reflectance': {
            'type': 'rgb',
            'value': [0.85, 0.85, 0.82]
        }
    }


def create_earth_bsdf(texture_path: Optional[str] = None, albedo: float = 0.3) -> Dict[str, Any]:
    """
    地球表面のBSDFを作成

    地球を Lambert 拡散とみなし、テクスチャがあれば bitmap、なければ
    アルベドに比例した RGB 一色（青系の海色）を割り当てる。
    平均アルベド 0.3（Bond アルベド）を基準にスケール。

    Args:
        texture_path: テクスチャ画像のパス（equirectangular 投影 PNG/JPG）
        albedo: アルベド（反射率、0.3 が地球平均値）

    Returns:
        Mitsuba BSDF辞書
    """
    bsdf = {
        'type': 'diffuse'
    }

    if texture_path:
        bsdf['reflectance'] = {
            'type': 'bitmap',
            'filename': texture_path
        }
    else:
        # デフォルトの地球色（海洋主体の青）
        bsdf['reflectance'] = {
            'type': 'rgb',
            'value': [0.15 * albedo / 0.3, 0.35 * albedo / 0.3, 0.65 * albedo / 0.3]
        }

    return bsdf


class MaterialLibrary:
    """材質ライブラリ

    宇宙機の典型的なパーツ（本体、アンテナ、太陽電池、MLI、ラジエーター、レンズ）
    に対する BSDF をセマンティック名で取得するファクトリ。
    シーン構築コードから材質詳細を隠蔽する役割。
    """

    @staticmethod
    def get_satellite_body_material(finish: str = 'brushed') -> Dict[str, Any]:
        """
        衛星本体の材質を取得

        Args:
            finish: 仕上げ ('mirror', 'polished', 'brushed', 'rough')

        Returns:
            Mitsuba BSDF辞書
        """
        roughness_map = {
            'mirror': 0.0,
            'polished': 0.05,
            'brushed': 0.2,
            'rough': 0.5
        }
        roughness = roughness_map.get(finish, 0.2)
        anisotropic = (finish == 'brushed')

        return create_aluminum_bsdf(roughness, anisotropic)

    @staticmethod
    def get_antenna_material() -> Dict[str, Any]:
        """アンテナの材質（金メッキ）"""
        return create_gold_bsdf(roughness=0.05)

    @staticmethod
    def get_solar_panel_material() -> Dict[str, Any]:
        """太陽電池パネルの材質"""
        return create_solar_panel_bsdf()

    @staticmethod
    def get_thermal_blanket_material() -> Dict[str, Any]:
        """多層断熱材（MLI）の材質"""
        return create_kapton_mli_bsdf()

    @staticmethod
    def get_radiator_material() -> Dict[str, Any]:
        """ラジエーター（黒色）の材質"""
        return create_black_paint_bsdf()

    @staticmethod
    def get_camera_lens_material(ior: float = 1.5) -> Dict[str, Any]:
        """カメラレンズの材質"""
        return create_glass_bsdf(ior, roughness=0.0)


def get_material_info(material_name: str) -> str:
    """
    材質の説明を取得

    Args:
        material_name: 材質名

    Returns:
        材質の説明文
    """
    info = {
        'aluminum': 'アルミニウム合金 - 衛星構造材として最も一般的。軽量で加工性が良い。',
        'gold': '金 - 電磁波反射、熱制御、腐食防止に使用。アンテナやMLIに使用。',
        'solar_panel': '太陽電池パネル - シリコンセル。光を吸収し電力に変換するため暗色。',
        'white_paint': '白色塗料 - 熱制御用。太陽光を反射し温度上昇を抑える。',
        'black_paint': '黒色塗料 - ラジエーター用。熱を効率的に放射。',
        'kapton_mli': 'カプトンMLI - 多層断熱材。金色の薄膜を多層化して真空断熱。',
        'carbon_composite': 'カーボンコンポジット - CFRP。軽量高強度構造材。',
        'beta_cloth': 'ベータクロス - 耐熱繊維。白色で熱反射性が高い。',
    }
    return info.get(material_name, '情報なし')


if __name__ == "__main__":
    # テスト
    print("光学材質モジュールのテスト\n")

    lib = MaterialLibrary()

    print("=== 衛星材質ライブラリ ===\n")

    materials = [
        ('衛星本体（ブラシ仕上げ）', lib.get_satellite_body_material('brushed')),
        ('アンテナ（金メッキ）', lib.get_antenna_material()),
        ('太陽電池パネル', lib.get_solar_panel_material()),
        ('多層断熱材（MLI）', lib.get_thermal_blanket_material()),
        ('ラジエーター', lib.get_radiator_material()),
    ]

    for name, bsdf in materials:
        print(f"{name}:")
        print(f"  BSDF Type: {bsdf.get('type', 'N/A')}")
        print()
