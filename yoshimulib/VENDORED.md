# yoshimulib（同梱版）について

このディレクトリは外部ライブラリ **yoshimulib** の一部を mRendering に
**同梱（vendoring）** したものです。以前は開発機のローカルパスへの symlink
でしたが、clone した人には壊れたリンクになるため実体をコピーしています。

## 出典

- リポジトリ: https://github.com/yyoshimula/yoshimulib （private）
- コミット: `91df58a29f89911d366ba778285e16cb32ea63cb`（2026-02-09）
- ライセンス: MIT（`LICENSE` をそのままコピー）
- 著者: Yasuhiro Yoshimura

## 同梱した部分集合

mRendering が実際に import するパッケージだけを入れています。

| パッケージ | 用途 |
|---|---|
| `attitude/` | クォータニオン・DCM・オイラー角・姿勢キネマティクス |
| `conversion/` | 単位・暦（`gc` 等）の変換 |
| `math_utils/` | skew 行列・`wrap_pi`・ルジャンドル多項式 |
| `time_utils/` | 時系変換（JD / MJD / UTC / TT・うるう秒） |
| `orbit/` | 軌道要素・ケプラー方程式・座標変換・恒星時・影関数・TLE |
| `sun_moon/` | 太陽・月の位置暦 |

## 除外したもの

- 上記以外の全パッケージ（`srp` `lightcurves` `environment` `ukf_ckf`
  `object` `gpr` `spherical_gaussian` `dual_quaternions`
  `geometric_integration` `orbit_determination` `relative_orbit` `hifi_srp`
  `spice` `utility`）— mRendering からは使っていない
- **`orbit/verify_mee.py`** — 非同梱の `srp` パッケージに依存するため。
  併せて `orbit/__init__.py` から該当 import と `__all__` エントリを削除した
- 本家の `README.md` / 検証レポート（`conversion_report*.md`）/ MATLAB
  スクリプト（`*.m`）/ `verification_logs/` / `tests/` / `requirements.txt`

上記に伴い、ルートの `__init__.py` は同梱 6 パッケージだけを import する
ように書き換えてある（`__version__` / `__author__` / `passfail` は本家のまま）。
依存は numpy と scipy のみで、どちらも mRendering の `requirements.txt` に既にある。

## 更新手順

本家に変更が入って取り込みたくなったら、本家 repo から該当ディレクトリを
再コピーして、上記の 2 点（ルート `__init__.py` の import 一覧、
`orbit/verify_mee.py` の除外）を適用し直す。

```bash
SRC=/path/to/yoshimulib
for d in attitude conversion math_utils time_utils orbit sun_moon; do
  rsync -a --exclude='__pycache__' --exclude='*.m' --exclude='*.md' "$SRC/$d/" "yoshimulib/$d/"
done
cp "$SRC/LICENSE" yoshimulib/LICENSE
rm -f yoshimulib/orbit/verify_mee.py
# → yoshimulib/__init__.py と yoshimulib/orbit/__init__.py の import を再調整
# → このファイルのコミットハッシュを更新
```
