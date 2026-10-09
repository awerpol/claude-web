#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
APP="$PROJECT_ROOT/dist/Claude Web.app"
CONTENTS="$APP/Contents"

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS"
printf 'APPL????' > "$CONTENTS/PkgInfo"

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>ClaudeWebLauncher</string>
    <key>CFBundleIdentifier</key><string>local.claude-web.app</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleName</key><string>Claude Web</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleSignature</key><string>????</string>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>CFBundleShortVersionString</key><string>2.4.3</string>
    <key>CFBundleVersion</key><string>2.4.3</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

MODULE_CACHE="${TMPDIR:-/tmp}/claude-web-swift-module-cache"
mkdir -p "$MODULE_CACHE"
swiftc -parse-as-library -module-cache-path "$MODULE_CACHE" -O -framework AppKit -framework WebKit \
    -o "$CONTENTS/MacOS/ClaudeWebLauncher" "$SCRIPT_DIR/ClaudeWebApp.swift"
chmod +x "$CONTENTS/MacOS/ClaudeWebLauncher"
plutil -lint "$CONTENTS/Info.plist"
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP"
printf 'Built: %s\n' "$APP"
