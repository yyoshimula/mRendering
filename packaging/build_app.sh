#!/usr/bin/env bash
# ダブルクリックで GUI を起動する mRender.app を dist/ に組み立てる。
#
#   bash packaging/build_app.sh        # → dist/mRender.app
#
# SatCap.app（PyInstaller で Python ごと同梱、配布用 124 MB）とは方針が違い、
# これは「このリポジトリの venv を使う軽量ランチャー」。Python も Mitsuba も
# 同梱しないので数百 KB で済むが、リポジトリと venv がこの Mac に必要。
# リポジトリの絶対パスをビルド時にランチャーへ埋め込むため、.app 自体は
# /Applications へ移動・コピーしても動く。
#
# 挙動（packaging/launcher_template.sh 参照）:
#   - ポート 8600 が既に応答すればウィンドウを開くだけ（二重起動防止）
#   - 未起動なら gui_server をバックグラウンド起動（ログ:
#     runs/_gui_configs/gui_server.log）→ 起動を待ってウィンドウ表示
#   - ウィンドウは Chrome/Edge/Brave の app モード（タブ無しのアプリ風）、
#     無ければ既定ブラウザ
#   - 停止はターミナルから: lsof -ti:8600 | xargs kill
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APP="dist/mRender.app"
ICON_SRC="${MRENDER_ICON_SRC:-build/app_icon_1024.png}"

step() { printf '\n=== %s ===\n' "$1"; }

# --- 1. アイコン（build/app_icon_1024.png → .icns） -----------------------
step "icon"
mkdir -p build
if [ ! -f "$ICON_SRC" ]; then
  echo "icon source $ICON_SRC が無いので生成します（衛星レンダのクロップ）"
  venv/bin/python packaging/make_icon.py
fi
ICONSET="build/mRender.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ICON_SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$ICON_SRC" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o build/mRender.icns

# --- 2. バンドル組み立て ----------------------------------------------------
step "bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp build/mRender.icns "$APP/Contents/Resources/"

sed "s|__REPO__|$REPO_ROOT|g" packaging/launcher_template.sh \
    > "$APP/Contents/MacOS/mRender"
chmod +x "$APP/Contents/MacOS/mRender"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>mRender</string>
  <key>CFBundleDisplayName</key><string>mRender</string>
  <key>CFBundleIdentifier</key><string>local.yoshimulab.mrender</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>mRender</string>
  <key>CFBundleIconFile</key><string>mRender</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

codesign --force --deep -s - "$APP" 2>/dev/null || true

# --- 3. スモークテスト ------------------------------------------------------
# ランチャーの構文チェックのみ（実起動はポートを取り合うので手動で確認する）
step "smoke"
bash -n "$APP/Contents/MacOS/mRender"
echo "OK: $APP"
echo "起動:   open $APP   （または Finder でダブルクリック）"
echo "停止:   lsof -ti:8600 | xargs kill"
