#!/usr/bin/env bash
set -euo pipefail

# GilzaBots Community uninstaller for direct Linux and macOS installations.
# Application files are removed by default. User data is removed only with
# the explicit --purge option.

INSTALL_DIR="$HOME/.local/share/GilzaBots"
BIN_DIR="$HOME/.local/bin"
BIN_LINK="$BIN_DIR/gilzabots"
UNINSTALL_LINK="$BIN_DIR/gilzabots-uninstall"
DESKTOP_FILE="$HOME/.local/share/applications/gilzabots.desktop"
PURGE=false
ASSUME_YES=false

usage() {
    printf '%s\n' \
        "Usage: gilzabots-uninstall [--purge] [--yes]" \
        "" \
        "  --purge  Also remove global GilzaBots settings, chats, caches, and credentials." \
        "           Project directories are never removed." \
        "  --yes    Confirm --purge without an interactive prompt."
}

for argument in "$@"; do
    case "$argument" in
        --purge) PURGE=true ;;
        --yes) ASSUME_YES=true ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$argument" >&2; usage >&2; exit 2 ;;
    esac
done

DATA_DIR=""
POINTER_FILE="$HOME/.gilzabotshome"
if $PURGE; then
    DATA_DIR="${GILZABOTS_HOME:-}"
    if [[ -z "$DATA_DIR" ]] && [[ -f "$POINTER_FILE" ]]; then
        DATA_DIR="$(command cat "$POINTER_FILE")"
    fi
    DATA_DIR="${DATA_DIR:-$HOME/.gilzabots}"

    case "$DATA_DIR" in
        /*) ;;
        *)
            printf 'Refusing to purge a relative data directory: %s\n' "$DATA_DIR" >&2
            exit 1
            ;;
    esac

    if [[ -d "$DATA_DIR" ]]; then
        CANONICAL_DATA_DIR="$(cd "$DATA_DIR" && pwd -P)"
        CANONICAL_USER_HOME="$(cd "$HOME" && pwd -P)"
    else
        CANONICAL_DATA_DIR="$DATA_DIR"
        CANONICAL_USER_HOME="$HOME"
    fi
    case "$CANONICAL_DATA_DIR" in
        ""|/|"$CANONICAL_USER_HOME")
            printf 'Refusing to purge unsafe data directory: %s\n' "$DATA_DIR" >&2
            exit 1
            ;;
    esac

    if ! $ASSUME_YES; then
        if ! exec 3<> /dev/tty; then
            printf '%s\n' "Cannot request confirmation. Re-run with --purge --yes after checking this path: $DATA_DIR" >&2
            exit 1
        fi
        printf 'Remove all GilzaBots global data at %s? Type "remove" to continue: ' "$DATA_DIR" >&3
        IFS= read -r confirmation <&3
        exec 3>&-
        if [[ "$confirmation" != "remove" ]]; then
            printf '%s\n' "Global data will be preserved."
            PURGE=false
        fi
    fi
fi

remove_managed_path_block() {
    local rc_file="$1"
    local expected_line="export PATH=\"$BIN_DIR:\$PATH\""
    local temp_file
    [[ -f "$rc_file" ]] || return 0
    grep -Fqx '# GilzaBots PATH' "$rc_file" || return 0
    temp_file="$(mktemp "${rc_file}.gilzabots.XXXXXX")"
    awk -v expected="$expected_line" '
        $0 == "# GilzaBots PATH" {
            if ((getline following) > 0 && following == expected) next
            print
            print following
            next
        }
        { print }
    ' "$rc_file" > "$temp_file"
    # Rewrite the existing file so its ownership and permissions stay intact.
    command cat "$temp_file" > "$rc_file"
    rm -f "$temp_file"
}

printf '%s\n' "Uninstalling the direct GilzaBots installation..."

# Do not delete an unrelated command or desktop entry that happens to use the
# same conventional name.
if [[ -L "$BIN_LINK" ]] && [[ "$(readlink "$BIN_LINK")" == "$INSTALL_DIR/GilzaBots" ]]; then
    rm -f "$BIN_LINK"
fi
# PyInstaller 6+ bundles this script under `_internal`; older releases kept it
# beside the executable.
if [[ -L "$UNINSTALL_LINK" ]]; then
    case "$(readlink "$UNINSTALL_LINK")" in
        "$INSTALL_DIR/uninstall.sh"|"$INSTALL_DIR/_internal/uninstall.sh")
            rm -f "$UNINSTALL_LINK"
            ;;
    esac
fi
if [[ -f "$DESKTOP_FILE" ]] && grep -Fqx "Exec=$INSTALL_DIR/GilzaBots" "$DESKTOP_FILE"; then
    rm -f "$DESKTOP_FILE"
fi

# The monitor daemon's login service (gilzabots/monitors/service.py): a
# systemd --user unit, or an XDG autostart entry where there is no user systemd.
# Removed only when it starts this installation.
MONITORS_UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/gilzabots-monitors.service"
MONITORS_AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/gilzabots-monitors.desktop"
if [[ -f "$MONITORS_UNIT" ]] && grep -Fq "$INSTALL_DIR/GilzaBots" "$MONITORS_UNIT"; then
    systemctl --user disable --now gilzabots-monitors.service >/dev/null 2>&1 || true
    rm -f "$MONITORS_UNIT"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi
if [[ -f "$MONITORS_AUTOSTART" ]] && grep -Fq "$INSTALL_DIR/GilzaBots" "$MONITORS_AUTOSTART"; then
    rm -f "$MONITORS_AUTOSTART"
fi
pkill -f "^$INSTALL_DIR/GilzaBots monitors " >/dev/null 2>&1 || true

# ~/.local/bin is shared by many applications. Remove our shell initialization
# block only while the directory has no other commands that may depend on it.
if [[ ! -d "$BIN_DIR" ]] || [[ -z "$(command ls -A "$BIN_DIR" 2>/dev/null)" ]]; then
    remove_managed_path_block "$HOME/.bashrc"
    remove_managed_path_block "$HOME/.zshrc"
    rmdir "$BIN_DIR" 2>/dev/null || true
else
    printf '%s\n' "Kept $BIN_DIR on PATH because it contains other commands."
fi

if $PURGE; then
    rm -rf -- "$DATA_DIR"
    rm -f -- "$POINTER_FILE"
    printf '%s\n' "Removed global data at $DATA_DIR. Project directories were preserved."
fi

rm -rf -- "$INSTALL_DIR"

printf '%s\n' "GilzaBots application files were removed."
if ! $PURGE; then
    printf '%s\n' "Global settings, chats, caches, credentials, and project directories were preserved."
fi
