#!/usr/bin/env bash
# DGX Spark 側で GUI サーバーを起動する（DGX 上で実行）。
#
#   tools/dgx/start_gui_dgx.sh                 # フォアグラウンド起動
#   GUI_PORT=8601 tools/dgx/start_gui_dgx.sh
#
# 127.0.0.1 バインドのまま起動し、Mac からは SSH トンネル
# （tools/dgx/connect_dgx.sh）越しにアクセスする。live_worker（ポート+1）は
# gui_server がプロキシするのでトンネルは GUI_PORT の 1 本だけでよい。
# AI drawer を使う場合は DGX 側に claude CLI が入っている必要がある。
set -euo pipefail

cd "$(dirname "$0")/../.."
source venv/bin/activate

# cuda_ad_rgb があれば GPU、無ければ llvm_ad_rgb (CPU) に自動フォールバック
export MRENDER_VARIANT="${MRENDER_VARIANT:-cuda_ad_rgb,llvm_ad_rgb}"
GUI_PORT="${GUI_PORT:-8600}"

echo "== GUI 起動: http://127.0.0.1:${GUI_PORT} (MRENDER_VARIANT=${MRENDER_VARIANT}) =="
exec python mrender.py gui --no-browser --host 127.0.0.1 --port "${GUI_PORT}"
