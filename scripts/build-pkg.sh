#!/bin/bash
#
# Copyright (C) 2026 by Fabrizio Giudici
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Builds the macOS installer package from the prepared VM ('make image'). It installs:
#
#   /usr/local/bin/BridgeBox             the command, with the settings of scripts/config.sh included
#   /usr/local/share/bridgebox/          the sealed VM (disk and ssh key) and vfkit, owned by root
#
# Nothing is started at installation: the VM starts only with 'BridgeBox start'. 'sudo BridgeBox uninstall' removes
# everything. Files written while the VM runs go to /tmp/bridgebox-<uid>.
#
set -euo pipefail

# shellcheck source=scripts/config.sh
source "$(cd "$(dirname "$0")" && pwd)/config.sh"

: "${VERSION:?VERSION must be set}"
PKG_IDENTIFIER="${PKG_IDENTIFIER:-it.tidalwave.BridgeBox}"
INSTALL_BIN=/usr/local/bin
INSTALL_SHARE=/usr/local/share/bridgebox
PKG_DIR="$ROOT/target/pkg"
PAYLOAD="$PKG_DIR/root"
OUTPUT="$ROOT/target/BridgeBox-$VERSION.pkg"

need pkgbuild
[ -f "$PROVISIONED" ] || die "the VM has not been prepared: run 'make image' first"
is_vm_running() { [ -f "$PID_FILE" ] && ps -p "$(cat "$PID_FILE")" -o comm= 2>/dev/null | grep -q vfkit; }
! is_vm_running || die "the VM is running: stop it first ('make stop')"

vfkit_path="$(command -v "$VFKIT" || true)"
[ -n "$vfkit_path" ] && [ -x "$vfkit_path" ] || die "vfkit not found: set VFKIT to its path"

log "Preparing the payload"
rm -rf "$PKG_DIR"
mkdir -p "$PAYLOAD$INSTALL_BIN" "$PAYLOAD$INSTALL_SHARE"

cp "$DISK" "$PAYLOAD$INSTALL_SHARE/disk.raw"
cp "$SSH_KEY" "$PAYLOAD$INSTALL_SHARE/id_ed25519"
touch "$PAYLOAD$INSTALL_SHARE/sealed"
cp "$vfkit_path" "$PAYLOAD$INSTALL_SHARE/vfkit"
cat > "$PAYLOAD$INSTALL_SHARE/NOTICE" << EOF
BridgeBox $VERSION

This directory contains vfkit (https://github.com/crc-org/vfkit), distributed under the Apache License, Version 2.0
(https://www.apache.org/licenses/LICENSE-2.0), and an Alpine Linux image (https://alpinelinux.org).
EOF

# The installed command is bin/BridgeBox with the settings of the installation and scripts/config.sh in place of the
# line that sources it.
command="$PAYLOAD$INSTALL_BIN/BridgeBox"
{
    awk -v config="$ROOT/scripts/config.sh" \
        -v prelude="INSTALLED=yes\nPKG_IDENTIFIER=$PKG_IDENTIFIER\nINSTALLED_COMMAND=$INSTALL_BIN/BridgeBox\nVM_DIR=$INSTALL_SHARE\nVFKIT=$INSTALL_SHARE/vfkit\nRUN_DIR=\"/tmp/bridgebox-\$(id -u)\"" '
        /^# shellcheck source=scripts\/config.sh$/ { next }
        /^source .*\/scripts\/config.sh"$/ {
            print "# Settings of the installation."
            print prelude
            print ""
            while ((getline line < config) > 0) print line
            close(config)
            next
        }
        { print }' "$ROOT/bin/BridgeBox"
} > "$command"
grep -q '^INSTALLED=yes$' "$command" || die "can't include the settings in the installed command"
bash -n "$command" || die "the installed command has syntax errors"

chmod 755 "$command" "$PAYLOAD$INSTALL_SHARE/vfkit"
chmod 644 "$PAYLOAD$INSTALL_SHARE/disk.raw" "$PAYLOAD$INSTALL_SHARE/id_ed25519" "$PAYLOAD$INSTALL_SHARE/sealed" \
          "$PAYLOAD$INSTALL_SHARE/NOTICE"
find "$PAYLOAD" -type d -exec chmod 755 {} +

log "Building $OUTPUT"
pkgbuild --root "$PAYLOAD" --identifier "$PKG_IDENTIFIER" --version "$VERSION" --install-location / \
         --ownership recommended "$OUTPUT"
log "Package built: $OUTPUT"
