#!/usr/bin/env bash
# Mac → DGX Spark へリポジトリを rsync する（Mac 側で実行）。
#
#   tools/dgx/sync_to_dgx.sh              # DGX_HOST（既定 dgx）へ同期
#   DGX_HOST=user@10.0.0.5 tools/dgx/sync_to_dgx.sh
#
# venv / runs / output / __pycache__ は同期しない（remote 側の runs/ は消さない）。
# assets/ と models/ は初回のみ重い（starfield.exr 274MB 等）が差分同期なので
# 2 回目以降は一瞬で終わる。
set -euo pipefail

DGX_HOST="${DGX_HOST:-dgx}"
DGX_DIR="${DGX_DIR:-mRendering}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "== rsync ${REPO_ROOT}/ -> ${DGX_HOST}:${DGX_DIR}/ =="
# --copy-unsafe-links: リポジトリ外を指す symlink があれば実体をコピーする
# （yoshimulib は 2026-09 に同梱へ移行したので現状は該当なし。将来の保険）
# internal/ は除外しない — 研究室内配布はこの同期経路で行う
rsync -avz --delete --copy-unsafe-links \
    --exclude 'venv/' \
    --exclude 'runs/' \
    --exclude 'output/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.git/' \
    --exclude '.DS_Store' \
    "${REPO_ROOT}/" "${DGX_HOST}:${DGX_DIR}/"

echo "== 完了。初回は続けて DGX 側で: cd ${DGX_DIR} && tools/dgx/setup_dgx.sh =="
