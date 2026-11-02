#!/usr/bin/env bash
# Build the OpalaTex snap with snapcraft's managed LXD instance.
#
# Usage: ./generate_snap.sh [--clean] [extra snapcraft pack arguments...]
#   --clean   run `snapcraft clean` first, discarding the cached build instance
#
# Why the firewall step exists: when Docker runs on the host it sets the IPv4
# FORWARD chain policy to DROP, which also drops traffic forwarded from LXD's
# bridge. The build container then only reaches hosts that answer over IPv6
# (archive.ubuntu.com does, so apt works), while IPv4-only hosts such as
# github.com time out — and the gnome extension clones
# https://github.com/canonical/gpu-snap.git, so the pull step fails with
# "exited with code 128". The rules below accept forwarding for the LXD bridge
# only. They are not persisted: they last until Docker or the host restarts,
# and this script re-applies them (idempotently) on every run.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

CLEAN=0
PACK_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        -h|--help)
            sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) PACK_ARGS+=("$arg") ;;
    esac
done

info() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

echo "=========================================="
echo "        OpalaTex - Snap Build             "
echo "=========================================="

# --- 1. Prerequisites --------------------------------------------------------
info "[1/4] Checking prerequisites..."

[ "$(uname -s)" = "Linux" ] || fail "Snaps can only be built on Linux."
[ "$(id -u)" -ne 0 ] || fail "Run this script as your regular user, not as root (snapcraft drives LXD as the user)."
[ -f snapcraft.yaml ] || fail "snapcraft.yaml not found in $(pwd)."
command -v snapcraft >/dev/null || fail "snapcraft is not installed. Install it with: sudo snap install snapcraft --classic"
command -v lxc >/dev/null || fail "LXD is not installed. Install it with: sudo snap install lxd && sudo lxd init --auto"

if ! lxc info >/dev/null 2>&1; then
    fail "Cannot talk to LXD. Make sure it is initialised (sudo lxd init --auto) and that your user is in the 'lxd' group (sudo usermod -aG lxd \$USER, then log in again)."
fi

# The bridge snapcraft's instances attach to is the parent of the default
# profile's NIC; fall back to LXD's standard name.
BRIDGE="$(lxc profile device get default eth0 network 2>/dev/null || true)"
[ -n "$BRIDGE" ] || BRIDGE="$(lxc profile device get default eth0 parent 2>/dev/null || true)"
[ -n "$BRIDGE" ] || BRIDGE="lxdbr0"
lxc network show "$BRIDGE" >/dev/null 2>&1 || fail "LXD network '$BRIDGE' does not exist. Run: sudo lxd init --auto"
echo "LXD bridge: $BRIDGE"

VERSION="$(sed -n 's/^version:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' snapcraft.yaml | head -1)"
echo "Snap version: ${VERSION:-unknown}"

# --- 2. Host firewall (Docker vs. LXD) --------------------------------------
info "[2/4] Checking that the host forwards IPv4 traffic for $BRIDGE..."

if ! command -v iptables >/dev/null && [ ! -x /usr/sbin/iptables ]; then
    echo "iptables not found; skipping the forwarding check."
else
    IPT="$(command -v iptables || echo /usr/sbin/iptables)"
    echo "Root access is needed to read and adjust the FORWARD chain."
    POLICY="$(sudo "$IPT" -S FORWARD | sed -n 's/^-P FORWARD //p')"
    if [ "$POLICY" != "DROP" ]; then
        echo "FORWARD policy is ${POLICY:-unknown}; no change needed."
    else
        # Docker evaluates DOCKER-USER before its own rules and never flushes
        # it, so that is the supported place for exceptions. Without Docker,
        # the rules go straight into FORWARD.
        if sudo "$IPT" -S DOCKER-USER >/dev/null 2>&1; then
            CHAIN="DOCKER-USER"
        else
            CHAIN="FORWARD"
        fi
        add_rule() {
            if sudo "$IPT" -C "$CHAIN" "$@" 2>/dev/null; then
                echo "  already present: $CHAIN $*"
            else
                sudo "$IPT" -I "$CHAIN" "$@"
                echo "  added: $CHAIN $*"
            fi
        }
        echo "FORWARD policy is DROP; allowing traffic for $BRIDGE in $CHAIN:"
        add_rule -i "$BRIDGE" -j ACCEPT
        add_rule -o "$BRIDGE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    fi
fi

# --- 3. Optional clean -------------------------------------------------------
if [ "$CLEAN" -eq 1 ]; then
    info "[3/4] Cleaning previous snapcraft build state..."
    snapcraft clean
else
    info "[3/4] Reusing the cached build instance (pass --clean to start fresh)."
fi

# --- 4. Build ----------------------------------------------------------------
info "[4/4] Running snapcraft pack..."
START_MARK="$(mktemp)"
trap 'rm -f "$START_MARK"' EXIT

if ! snapcraft pack ${PACK_ARGS[@]+"${PACK_ARGS[@]}"}; then
    LOG="$(find "$HOME/.local/state/snapcraft/log" -name '*.log' -newer "$START_MARK" 2>/dev/null | sort | tail -1)"
    if [ -n "$LOG" ] && grep -qE "Failed to connect to|Could not resolve host|Connection timed out" "$LOG"; then
        printf '\nThe build container could not reach the network (see %s).\n' "$LOG" >&2
        printf 'Check for another firewall (ufw, a VPN, a corporate proxy) blocking traffic from %s.\n' "$BRIDGE" >&2
    fi
    fail "snapcraft pack failed${LOG:+. Full log: $LOG}"
fi

SNAP_FILE="opalatex_${VERSION}_amd64.snap"
if [ -f "$SNAP_FILE" ]; then
    info "Snap built: $(pwd)/$SNAP_FILE"
    echo "Install it locally with: sudo snap install --dangerous ./$SNAP_FILE"
else
    info "snapcraft pack finished. Built snaps:"
    ls -1t ./*.snap 2>/dev/null | head -3
fi
