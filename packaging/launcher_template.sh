#!/bin/bash
# mRender.app のランチャー本体（build_app.sh が __REPO__ を実パスに置換して
# .app 内へコピーする。このテンプレート自体は実行しない）。
#
# ダブルクリック → gui_server をバックグラウンド起動（既に動いていれば再利用）
# → アプリ風ウィンドウ（Chrome 系 app モード、無ければ既定ブラウザ）を開く。
# LSUIElement=1 なので Dock には常駐しない。サーバー停止は:
#   lsof -ti:8600 | xargs kill
REPO="__REPO__"
PORT=8600
URL="http://127.0.0.1:${PORT}"
LOG="$REPO/runs/_gui_configs/gui_server.log"

alive() { curl -s -o /dev/null --max-time 1 "$URL/"; }

open_window() {
  local b
  for b in "Google Chrome" "Microsoft Edge" "Brave Browser" "Chromium"; do
    if [ -d "/Applications/$b.app" ]; then
      open -na "$b" --args --app="$URL"
      return
    fi
  done
  open "$URL"
}

if alive; then
  open_window
  exit 0
fi

if [ ! -x "$REPO/venv/bin/python" ]; then
  osascript -e 'display alert "mRender" message "venv が見つかりません。リポジトリで python3 -m venv venv && pip install -r requirements.txt を実行してください。"'
  exit 1
fi

mkdir -p "$(dirname "$LOG")"
cd "$REPO"
nohup "$REPO/venv/bin/python" mrender.py gui --no-browser --port "$PORT" \
    >> "$LOG" 2>&1 &

for _ in $(seq 1 100); do
  alive && break
  sleep 0.2
done

if alive; then
  open_window
else
  osascript -e "display alert \"mRender\" message \"GUI サーバーが起動しませんでした。ログ: $LOG\""
  exit 1
fi
