#!/usr/bin/env bash
# DGX Spark 側の初回セットアップ（DGX 上のリポジトリルートで実行）。
#
#   tools/dgx/setup_dgx.sh
#
# venv 作成 → 依存インストール → Mitsuba の import / バリアント確認まで行う。
# Grace（aarch64 Linux）向けの mitsuba pip wheel が無い場合や cuda_ad_rgb を
# 使いたい場合はソースビルドが必要（tools/dgx/README.md 参照）。
set -euo pipefail

cd "$(dirname "$0")/../.."

if [ ! -d venv ]; then
    echo "== venv 作成 =="
    python3 -m venv venv
fi
source venv/bin/activate
pip install -U pip

echo "== 依存インストール =="
# mitsuba は wheel が無い環境（aarch64 Linux）だと失敗しうるので分離して試す
grep -v '^mitsuba' requirements.txt > /tmp/req_nomitsuba.txt
pip install -r /tmp/req_nomitsuba.txt
if ! pip install "$(grep '^mitsuba' requirements.txt)"; then
    echo ""
    echo "!! mitsuba の pip インストールに失敗（この CPU アーキテクチャ用 wheel が"
    echo "!! 無い可能性が高い）。tools/dgx/README.md の手順でソースビルドして、"
    echo "!! build ディレクトリを PYTHONPATH に通すか 'pip install .' すること。"
fi

echo "== Mitsuba バリアント確認 =="
python - <<'EOF'
try:
    import mitsuba as mi
except ImportError as e:
    print(f"mitsuba を import できない: {e}")
    raise SystemExit(1)
print("利用可能バリアント:", mi.variants())
if 'cuda_ad_rgb' in mi.variants():
    print("→ cuda_ad_rgb が使える。start_gui_dgx.sh の既定で GPU レンダリングになる")
else:
    print("→ cuda_ad_rgb が無い。GPU を使うにはソースビルドで有効化する"
          "（README 参照）。それまでは llvm_ad_rgb (CPU) で動く")
EOF

echo "== セットアップ完了。起動は: tools/dgx/start_gui_dgx.sh =="
