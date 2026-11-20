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
# Prepares the BridgeBox VM in $VM_DIR:
#
#   1. downloads the Alpine cloud image (cached in $CACHE_DIR), extracts the disk, creates the ssh key and the
#      cloud-init files;
#   2. boots the VM and, with scripts/provision.sh (as the default user of the image), creates the user that
#      bin/BridgeBox logs in as, installs the btrfs and ZFS software, Avahi, which announces the VM with Bonjour, and
#      Samba, which shares it with the Mac (internet access is needed); installs the back-end
#      (vm/bridgebox-backend.py), which mounts the disks for bin/BridgeBox; then seals the image with scripts/seal.sh,
#      so that from then on the VM runs from a read-only disk and loses its changes when it stops;
#   3. boots the sealed VM, to check that it runs from a read-only disk, that the ZFS and btrfs kernel modules load
#      with the upgraded kernel, that Samba is running and that the VM is announced with Bonjour.
#
# If a step fails, running it again resumes from that step (provisioning is idempotent).
# The VM is then controlled with bin/BridgeBox.
#
set -euo pipefail

# shellcheck source=scripts/config.sh
source "$(cd "$(dirname "$0")" && pwd)/config.sh"

BRIDGEBOX="$ROOT/bin/BridgeBox"

download_image() {
    local image="$CACHE_DIR/$ALPINE_IMAGE"
    local url="$ALPINE_MIRROR/v${ALPINE_VERSION%.*}/releases/cloud/$ALPINE_IMAGE"

    if [ ! -f "$image" ]; then
        log "Downloading $url"
        curl -fL --progress-bar -o "$image.part" "$url"
        mv "$image.part" "$image"
    fi

    log "Verifying the checksum of $ALPINE_IMAGE"
    local expected actual
    expected="$(curl -fsSL "$url.sha512" | awk '{print $1}')"
    actual="$(shasum -a 512 "$image" | awk '{print $1}')"
    if [ "$expected" != "$actual" ]; then
        rm -f "$image"
        die "checksum mismatch: the download has been deleted, please retry"
    fi
}

create_disk() {
    download_image

    # The archive contains a single file (disk.raw, as Google Cloud requires).
    log "Extracting the disk image"
    local work="$VM_DIR/extract" content
    rm -rf "$work"
    mkdir -p "$work"
    tar -xzf "$CACHE_DIR/$ALPINE_IMAGE" -C "$work"
    content="$(ls -A "$work")"
    [ "$(echo "$content" | wc -l | tr -d ' ')" = "1" ] && [ -f "$work/$content" ] \
        || die "unexpected content in $ALPINE_IMAGE: $content"

    if [ ! -f "$SSH_KEY" ]; then
        log "Creating the ssh key"
        ssh-keygen -q -t ed25519 -N '' -C bridgebox-dev -f "$SSH_KEY"
    fi

    log "Creating the cloud-init files"
    mkdir -p "$CLOUD_INIT_DIR"
    sed "s|@SSH_PUBLIC_KEY@|$(cat "$SSH_KEY.pub")|" "$ROOT/cloud-init/user-data" > "$CLOUD_INIT_DIR/user-data"
    cp "$ROOT/cloud-init/meta-data" "$CLOUD_INIT_DIR/meta-data"

    rm -f "$EFI_VARS" "$KNOWN_HOSTS"
    mv "$work/$content" "$DISK"
    rm -rf "$work"
}

# Shuts down the VM from inside, so that the disk is cleanly unmounted, and waits for vfkit to exit; 'BridgeBox stop'
# then only cleans up (or forces the VM off, if it didn't stop). There are no disks to unmount here.
shutdown_vm() {
    "$BRIDGEBOX" ssh 'doas poweroff' || true
    local deadline=$((SECONDS + STOP_TIMEOUT))
    while [ "$("$BRIDGEBOX" status)" != "not running" ] && [ $SECONDS -lt $deadline ]; do
        sleep 1
    done
    "$BRIDGEBOX" stop
}

# Runs as PROVISION_USER: SSH_USER is created by scripts/provision.sh.
provision() {
    (
        export SSH_USER="$PROVISION_USER"
        log "Installing the btrfs and ZFS software"
        "$BRIDGEBOX" start
        "$BRIDGEBOX" ssh 'doas sh -s' < "$ROOT/scripts/provision.sh"
        log "Installing the back-end as $BACKEND"
        "$BRIDGEBOX" ssh "doas sh -c 'mkdir -p $(dirname "$BACKEND") && cat > $BACKEND && chmod 755 $BACKEND'" \
            < "$ROOT/vm/bridgebox-backend.py"
        "$BRIDGEBOX" ssh 'doas sh -s' < "$ROOT/scripts/seal.sh"
        shutdown_vm
    )
    touch "$SEALED"
}

# Boots the sealed VM: bin/BridgeBox attaches the disk read-only and finds the VM with Bonjour. It logs in as SSH_USER,
# so this also checks that the user created by scripts/provision.sh can log in and use doas.
verify() {
    log "Booting the sealed VM to check it"
    "$BRIDGEBOX" start
    log "$MDNS_NAME resolves to $("$BRIDGEBOX" ip)"
    "$BRIDGEBOX" ssh 'printf "logged in as " && id -un && grep -q " / overlay " /proc/mounts \
        && echo "the root filesystem is an overlay" \
        && uname -r && doas modprobe zfs && doas modprobe btrfs && doas zfs version && btrfs --version \
        && doas rc-service avahi-daemon status'
    "$BRIDGEBOX" ssh "doas $BACKEND unmount && echo 'the back-end runs'"
    "$BRIDGEBOX" ssh "doas $BACKEND samba start && doas rc-service samba status && doas $BACKEND samba stop"
    "$BRIDGEBOX" ssh "doas $BACKEND nfs start && doas rc-service bridgebox-nfs status \
        && pidof ganesha.nfsd > /dev/null && echo 'nfs-ganesha runs' && doas $BACKEND nfs stop"
    shutdown_vm
}

prepare() {
    need curl shasum tar ssh-keygen
    check_paths
    mkdir -p "$CACHE_DIR" "$VM_DIR"

    if [ -f "$PROVISIONED" ]; then
        log "The VM is already prepared in $VM_DIR: nothing to do ('make clean' to start over)"
        return
    fi

    [ -f "$DISK" ] || create_disk
    [ -f "$SEALED" ] || provision
    verify
    touch "$PROVISIONED"
    log "VM prepared in $VM_DIR"
}

prepare
