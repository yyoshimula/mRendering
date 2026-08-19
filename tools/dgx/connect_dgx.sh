#!/usr/bin/env bash
# Mac 側から DGX Spark の GUI に接続する（Mac 上で実行）。
#
#   tools/dgx/connect_dgx.sh          # GUI 起動確認 → トンネル → ブラウザを開く
#   tools/dgx/connect_dgx.sh stop     # トンネルを閉じる
#   DGX_HOST=user@10.0.0.5 tools/dgx/connect_dgx.sh
#
# リモート GUI が起動していなければ tmux セッション mrender-gui で自動起動する
# （tmux が無ければ nohup フォールバック）。トンネルは 8600 の 1 本のみ。
set -euo pipefail

DGX_HOST="${DGX_HOST:-dgx}"
DGX_DIR="${DGX_DIR:-mRendering}"
GUI_PORT="${GUI_PORT:-8600}"
TUNNEL_TAG="mrender-dgx-tunnel-${GUI_PORT}"

if [ "${1:-}" = "stop" ]; then
    pkill -f "${TUNNEL_TAG}" 2>/dev/null && echo "トンネルを閉じた" || echo "トンネルは開いていない"
    exit 0
fi

echo "== リモート GUI の起動確認 (${DGX_HOST}) =="
if ! ssh "${DGX_HOST}" "curl -sf -o /dev/null --max-time 3 http://127.0.0.1:${GUI_PORT}/"; then
    echo "GUI が起動していないので起動する"
    ssh "${DGX_HOST}" "cd ${DGX_DIR} && if command -v tmux >/dev/null; then \
        tmux has-session -t mrender-gui 2>/dev/null || \
        tmux new-session -d -s mrender-gui \"GUI_PORT=${GUI_PORT} tools/dgx/start_gui_dgx.sh\"; \
      else \
        mkdir -p runs/_gui_configs && \
        nohup env GUI_PORT=${GUI_PORT} tools/dgx/start_gui_dgx.sh \
          > runs/_gui_configs/gui_server_dgx.log 2>&1 & \
      fi"
    # Mitsuba 常駐の立ち上がりを待つ
    for _ in $(seq 1 15); do
        sleep 1
        ssh "${DGX_HOST}" "curl -sf -o /dev/null --max-time 3 http://127.0.0.1:${GUI_PORT}/" && break
    done
fi

echo "== SSH トンネル 127.0.0.1:${GUI_PORT} -> ${DGX_HOST}:${GUI_PORT} =="
if pgrep -f "${TUNNEL_TAG}" >/dev/null; then
    echo "既存のトンネルを再利用"
else
    # -o で自前タグを埋め込み、stop 時に pkill -f で特定できるようにする
    ssh -f -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
        -o "Tag=${TUNNEL_TAG}" \
        -L "${GUI_PORT}:127.0.0.1:${GUI_PORT}" "${DGX_HOST}" \
        || { echo "トンネル確立に失敗"; exit 1; }
fi

echo "== ブラウザを開く: http://127.0.0.1:${GUI_PORT} =="
open "http://127.0.0.1:${GUI_PORT}"
