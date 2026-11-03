#!/usr/bin/env bash
set -euo pipefail

# GilzaBots Community installer for Linux and macOS.

echo "=========================================="
echo "      GilzaBots Installer (Community)      "
echo "=========================================="

INSTALL_DIR="$HOME/.local/share/GilzaBots"
BIN_DIR="$HOME/.local/bin"
REPO_OWNER="gilzamir18"
REPO_NAME="GilzaBots"
# `HEAD` lets raw.githubusercontent.com resolve the repository's default branch,
# so this installer does not break every time the release branch is renamed.
# A literal branch name here is what made the fallback fetch 404.
UNINSTALLER_REF="HEAD"

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"

case "$OS_NAME" in
    Linux)
        ASSET_NAME="GilzaBots-linux-x64.tar.gz"
        TEMP_FILE="/tmp/gilzabots_release.tar.gz"
        ;;
    Darwin)
        ASSET_NAME="GilzaBots-macos-x64.zip"
        TEMP_FILE="/tmp/gilzabots_release.zip"
        ;;
    *)
        echo "Unsupported operating system: $OS_NAME" >&2
        exit 1
        ;;
esac

case "$ARCH_NAME" in
    x86_64|amd64)
        ;;
    *)
        echo "Unsupported CPU architecture: $ARCH_NAME. This installer currently requires an x64 release asset." >&2
        exit 1
        ;;
esac

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install GilzaBots." >&2
    exit 1
fi

if [[ "$ASSET_NAME" == *.zip ]] && ! command -v unzip >/dev/null 2>&1; then
    echo "unzip is required to install GilzaBots on macOS." >&2
    exit 1
fi

download_with_retry() {
    local url="$1"
    local output="$2"
    local max_attempts=3
    local attempt

    for attempt in $(seq 1 "$max_attempts"); do
        rm -f "$output"
        if curl -fL --retry 2 --retry-delay 2 \
            -H "Accept: application/octet-stream" \
            -H "User-Agent: GilzaBots-Installer" \
            "$url" -o "$output"; then
            if [[ -s "$output" ]]; then
                return 0
            fi
        fi

        if [[ "$attempt" -eq "$max_attempts" ]]; then
            echo "Failed to download GilzaBots after $max_attempts attempts." >&2
            return 1
        fi

        echo "Download failed. Retrying ($attempt/$max_attempts)..."
        sleep $((2 * attempt))
    done
}

get_download_url() {
    if [[ -n "${GILZABOTS_DOWNLOAD_URL:-}" ]]; then
        if [[ "$GILZABOTS_DOWNLOAD_URL" == *"/actions/runs/"*"/artifacts/"* ]]; then
            echo "GILZABOTS_DOWNLOAD_URL points to a GitHub Actions artifact. Use a GitHub Release asset URL instead." >&2
            return 1
        fi
        printf '%s\n' "$GILZABOTS_DOWNLOAD_URL"
        return 0
    fi

    local latest_url="https://github.com/$REPO_OWNER/$REPO_NAME/releases/latest/download/$ASSET_NAME"
    printf '%s\n' "$latest_url"
}

DOWNLOAD_URL="$(get_download_url)"

echo "Downloading GilzaBots from: $DOWNLOAD_URL"
if ! download_with_retry "$DOWNLOAD_URL" "$TEMP_FILE"; then
    echo "The latest GitHub release must contain $ASSET_NAME before this installer can run." >&2
    exit 1
fi

echo "Preparing install directory at $INSTALL_DIR..."
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

echo "Extracting files..."
case "$TEMP_FILE" in
    *.tar.gz)
        tar -xzf "$TEMP_FILE" -C "$INSTALL_DIR" --strip-components=1 2>/dev/null ||
            tar -xzf "$TEMP_FILE" -C "$INSTALL_DIR"
        ;;
    *.zip)
        unzip -q -o "$TEMP_FILE" -d "$INSTALL_DIR"
        ;;
    *)
        echo "Unsupported archive format: $TEMP_FILE" >&2
        exit 1
        ;;
esac

if [[ -d "$INSTALL_DIR/GilzaBots" && -f "$INSTALL_DIR/GilzaBots/GilzaBots" ]]; then
    find "$INSTALL_DIR/GilzaBots" -mindepth 1 -maxdepth 1 -exec mv -f {} "$INSTALL_DIR/" \;
    rmdir "$INSTALL_DIR/GilzaBots" 2>/dev/null || true
fi

if [[ ! -f "$INSTALL_DIR/GilzaBots" ]]; then
    echo "The downloaded package does not contain the GilzaBots executable." >&2
    exit 1
fi

# PyInstaller 6+ places bundled data files (icon, uninstaller, ...) in an
# `_internal` contents directory next to the executable; older releases kept
# them beside it. Resolve both layouts instead of assuming one.
find_bundled_file() {
    local name="$1"
    local candidate
    for candidate in "$INSTALL_DIR/$name" "$INSTALL_DIR/_internal/$name"; do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# The uninstaller is a convenience, not part of the application. A release that
# predates it, or a branch that does not carry it yet, must not abort an
# installation whose payload is already unpacked: warn and continue, and only
# publish the `gilzabots-uninstall` entry point when the script is really there.
UNINSTALLER_PATH="$(find_bundled_file uninstall.sh || true)"
if [[ -z "$UNINSTALLER_PATH" ]]; then
    echo "This release predates the bundled uninstaller; downloading it separately..."
    if download_with_retry \
        "https://raw.githubusercontent.com/$REPO_OWNER/$REPO_NAME/$UNINSTALLER_REF/uninstall.sh" \
        "$INSTALL_DIR/uninstall.sh"; then
        UNINSTALLER_PATH="$INSTALL_DIR/uninstall.sh"
    else
        rm -f "$INSTALL_DIR/uninstall.sh"
        echo "Could not fetch the GilzaBots uninstaller; continuing without it." >&2
        echo "To remove GilzaBots later, delete $INSTALL_DIR and $BIN_DIR/gilzabots." >&2
    fi
fi

echo "Creating command symlink in $BIN_DIR..."
mkdir -p "$BIN_DIR"
ln -sfn "$INSTALL_DIR/GilzaBots" "$BIN_DIR/gilzabots"
chmod +x "$INSTALL_DIR/GilzaBots" "$BIN_DIR/gilzabots" 2>/dev/null || true
if [[ -n "$UNINSTALLER_PATH" ]]; then
    ln -sfn "$UNINSTALLER_PATH" "$BIN_DIR/gilzabots-uninstall"
    chmod +x "$UNINSTALLER_PATH" "$BIN_DIR/gilzabots-uninstall" 2>/dev/null || true
fi

DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"

# A desktop entry whose Icon points at a missing file renders as a blank tile
# in the launcher, so only write the key for an icon that really exists.
ICON_LINE=""
if ICON_PATH="$(find_bundled_file icon.png)"; then
    ICON_LINE="Icon=$ICON_PATH"
else
    echo "The downloaded package does not contain icon.png; the launcher entry will have no icon." >&2
fi

cat > "$DESKTOP_DIR/gilzabots.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=GilzaBots
Comment=GilzaBots Open-Source AI LaTeX IDE
Exec=$INSTALL_DIR/GilzaBots
$ICON_LINE
Terminal=false
Categories=Utility;Development;TextEditor;
EOF

chmod +x "$DESKTOP_DIR/gilzabots.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "Adding $BIN_DIR to PATH..."
    if [[ -f "$HOME/.bashrc" ]] && ! grep -q "$BIN_DIR" "$HOME/.bashrc"; then
        printf '\n# GilzaBots PATH\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$HOME/.bashrc"
    fi
    if [[ -f "$HOME/.zshrc" ]] && ! grep -q "$BIN_DIR" "$HOME/.zshrc"; then
        printf '\n# GilzaBots PATH\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$HOME/.zshrc"
    fi
fi

rm -f "$TEMP_FILE"

echo ""
echo "=========================================="
echo "   GilzaBots installed successfully!       "
echo "=========================================="
echo "Terminal command: gilzabots"
echo "Application launcher created."
if [[ -n "$UNINSTALLER_PATH" ]]; then
    echo "To uninstall: gilzabots-uninstall"
fi
