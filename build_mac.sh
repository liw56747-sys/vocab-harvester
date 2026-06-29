#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# build_mac.sh — macOS build & package script for vocab-harvester
#
# Usage:
#   ./build_mac.sh                    # auto-bump patch version, build, create DMG
#   ./build_mac.sh --bump minor       # bump minor version
#   ./build_mac.sh --bump major       # bump major version
#   ./build_mac.sh --no-bump          # keep current version
#   ./build_mac.sh --skip-tests       # skip unit tests
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/VERSION"
DIST_DIR="$SCRIPT_DIR/dist/vocab-harvester"
INSTALLER_DIR="$SCRIPT_DIR/installer"
APP_NAME="vocab-harvester"

# ── Defaults ──
BUMP_PART="patch"
NO_BUMP=false
SKIP_TESTS=false

# ── Parse arguments ──
while [[ $# -gt 0 ]]; do
    case "$1" in
        --bump)
            shift
            BUMP_PART="${1:-patch}"
            if [[ "$BUMP_PART" != "major" && "$BUMP_PART" != "minor" && "$BUMP_PART" != "patch" ]]; then
                echo "[ERROR] --bump must be one of: major, minor, patch"
                exit 1
            fi
            shift
            ;;
        --no-bump)
            NO_BUMP=true
            shift
            ;;
        --skip-tests)
            SKIP_TESTS=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--bump major|minor|patch] [--no-bump] [--skip-tests]"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: $1"
            exit 1
            ;;
    esac
done

# ── Version management ──
read_version() {
    if [[ -f "$VERSION_FILE" ]]; then
        cat "$VERSION_FILE" | tr -d '[:space:]'
    else
        echo "0.0.0"
    fi
}

bump_version() {
    local current="$1"
    local part="$2"
    IFS='.' read -r major minor patch <<< "$current"
    case "$part" in
        major) echo "$((major + 1)).0.0" ;;
        minor) echo "$major.$((minor + 1)).0" ;;
        patch) echo "$major.$minor.$((patch + 1))" ;;
    esac
}

CURRENT_VERSION="$(read_version)"

if [[ "$NO_BUMP" == true ]]; then
    NEW_VERSION="$CURRENT_VERSION"
else
    NEW_VERSION="$(bump_version "$CURRENT_VERSION" "$BUMP_PART")"
fi

echo "$NEW_VERSION" > "$VERSION_FILE"

if [[ "$NO_BUMP" == true ]]; then
    echo "[*] Version: $NEW_VERSION (unchanged)"
else
    echo "[*] Version: $CURRENT_VERSION -> $NEW_VERSION"
fi

# ── Ensure macOS ──
if [[ "$(uname)" != "Darwin" ]]; then
    echo "[WARN] This script is designed for macOS. Proceeding anyway..."
fi

# ── Run unit tests ──
if [[ "$SKIP_TESTS" == false ]]; then
    echo ""
    echo "[*] Running unit tests..."
    if python3 -m pytest tests/ -x -q --tb=short -m "not integration"; then
        echo "[OK] Tests passed"
    else
        echo "[!] Tests failed. Use --skip-tests to skip."
        exit 1
    fi
else
    echo "[*] Skipping tests"
fi

# ── Clean old build artifacts ──
echo ""
echo "[*] Cleaning old build..."
rm -rf "$SCRIPT_DIR/dist" "$SCRIPT_DIR/build"
echo "[OK] Clean done"

# ── PyInstaller ──
echo ""
echo "[*] Running PyInstaller (build_mac.spec)..."
python3 -m PyInstaller build_mac.spec --noconfirm

if [[ ! -d "$DIST_DIR" ]]; then
    echo "[FAIL] PyInstaller did not produce output directory: $DIST_DIR"
    exit 1
fi

DIST_SIZE=$(du -sh "$DIST_DIR" | cut -f1)
echo "[OK] Build complete ($DIST_SIZE)"

# ── Create .app bundle for DMG ──
# PyInstaller onedir on macOS produces a directory; we wrap it as a .app
APP_BUNDLE="$SCRIPT_DIR/dist/${APP_NAME}.app"

if [[ ! -d "$APP_BUNDLE" ]]; then
    echo "[*] Creating .app bundle..."
    mkdir -p "$APP_BUNDLE/Contents/MacOS"
    mkdir -p "$APP_BUNDLE/Contents/Resources"

    # Move the built files into the bundle
    cp -R "$DIST_DIR"/* "$APP_BUNDLE/Contents/MacOS/"

    # Copy icon
    if [[ -f "$SCRIPT_DIR/icon.icns" ]]; then
        cp "$SCRIPT_DIR/icon.icns" "$APP_BUNDLE/Contents/Resources/"
    fi

    # Create Info.plist
    cat > "$APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleName</key>
    <string>vocab-harvester</string>
    <key>CFBundleIdentifier</key>
    <string>com.vocabharvester.app</string>
    <key>CFBundleVersion</key>
    <string>${NEW_VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${NEW_VERSION}</string>
    <key>CFBundleIconFile</key>
    <string>icon.icns</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
</dict>
</plist>
PLIST
    echo "[OK] .app bundle created"
fi

# ── Create DMG installer ──
echo ""
echo "[*] Creating DMG installer..."
mkdir -p "$INSTALLER_DIR"

DMG_NAME="${APP_NAME}-${NEW_VERSION}-mac.dmg"
DMG_PATH="$INSTALLER_DIR/$DMG_NAME"
DMG_STAGING="$SCRIPT_DIR/dist/dmg_staging"

# Prepare staging directory
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "$APP_BUNDLE" "$DMG_STAGING/"
# Add symlink to /Applications for drag-to-install
ln -s /Applications "$DMG_STAGING/Applications"

# Try create-dmg first, fall back to hdiutil
if command -v create-dmg &>/dev/null; then
    echo "[*] Using create-dmg..."
    create-dmg \
        --volname "vocab-harvester" \
        --volicon "$SCRIPT_DIR/icon.icns" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "${APP_NAME}.app" 150 180 \
        --app-drop-link 450 180 \
        --no-internet-enable \
        "$DMG_PATH" \
        "$DMG_STAGING"
    echo "[OK] DMG created with create-dmg"
else
    echo "[*] create-dmg not found, using hdiutil..."
    hdiutil create \
        -volname "vocab-harvester" \
        -srcfolder "$DMG_STAGING" \
        -ov \
        -format UDZO \
        "$DMG_PATH"
    echo "[OK] DMG created with hdiutil"
fi

# Clean up staging
rm -rf "$DMG_STAGING"

# ── Summary ──
DMG_SIZE=$(du -sh "$DMG_PATH" | cut -f1)
echo ""
echo "=================================================="
echo "  vocab-harvester v${NEW_VERSION} macOS build complete!"
echo "  app:     $APP_BUNDLE"
echo "  dmg:     $DMG_PATH ($DMG_SIZE)"
echo "=================================================="
