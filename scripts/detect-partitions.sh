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
# TEMPORARY: lists the partitions of external disks and detects btrfs and ZFS by reading their on-disk signatures,
# since macOS doesn't recognise those filesystems (and the GPT partition type can be wrong or generic).
#
#   sudo scripts/detect-partitions.sh            scans all the external physical disks
#   sudo scripts/detect-partitions.sh disk4      scans only the given disks
#
# It only reads from the disks. Root is needed because macOS allows only root to read /dev/disk*.
# Only tools that ship with macOS are used; compatible with bash 3.2.
#
#   btrfs: the superblock is at 64 KiB from the start of the partition, with the magic "_BHRfS_M" at offset 0x40,
#          the filesystem UUID at 0x20, the number of devices at 0x88, the device id at 0xC9 and the label at 0x12B.
#   ZFS:   the first vdev label (256 KiB) is at the start of the partition: its second half is the uberblock array,
#          where each uberblock starts with the magic 0x00bab10c at a 1 KiB boundary; the first half contains an
#          nvlist with the pool name.
#
set -euo pipefail

# The data read from the disks is binary: without this, the macOS tr fails with "Illegal byte sequence".
export LC_ALL=C

die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

[ "$(id -u)" = "0" ] || die "run with sudo: macOS allows only root to read the raw disks"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Prints COUNT bytes at OFFSET of FILE as a hex string (hexdump with an explicit format, since od pads differently
# on macOS and Linux).
hex() { hexdump -v -s "$2" -n "$3" -e '1/1 "%02x"' "$1"; }

# Prints the little-endian unsigned 64 bit integer at OFFSET of FILE (macOS on Apple Silicon is little-endian too).
u64() { od -A n -t u8 -j "$2" -N 8 "$1" | tr -d ' \n'; }

# Prints the details of a btrfs filesystem on the given raw device, or returns 1 if there's none.
btrfs_info() {
    local sb="$TMP/btrfs-sb"
    dd if="$1" of="$sb" bs=4096 skip=16 count=1 2>/dev/null || return 1   # read errors are reported by scan()
    [ "$(dd if="$sb" bs=1 skip=64 count=8 2>/dev/null | tr -d '\000')" = "_BHRfS_M" ] || return 1

    local uuid label devices devid
    uuid="$(hex "$sb" 32 16 | sed -E 's/(.{8})(.{4})(.{4})(.{4})(.{12})/\1-\2-\3-\4-\5/')"
    label="$(dd if="$sb" bs=1 skip=299 count=256 2>/dev/null | tr -d '\000')"
    devices="$(u64 "$sb" 136)"
    devid="$(u64 "$sb" 201)"
    echo "btrfs label='$label' uuid=$uuid device=$devid/$devices"
}

# Prints the details of a ZFS pool member on the given raw device, or returns 1 if there's none.
zfs_info() {
    local label="$TMP/zfs-label"
    dd if="$1" of="$label" bs=262144 count=1 2>/dev/null || return 1

    # od prints the input offset in decimal followed by two 64 bit words per line (host endianness): an uberblock
    # starts a line at a 1 KiB boundary. Both byte orders are accepted; leading zeros are optional, since the macOS
    # od doesn't print them.
    od -A d -t x8 -v -j 131072 -N 131072 "$label" \
        | awk '($1 % 1024) == 0 && ($2 ~ /^0*bab10c$/ || $2 ~ /^0*cb1ba0000000000$/) { found = 1 }
               END { exit !found }' || return 1

    # In the nvlist the string "name" is followed by the pool name; other bytes in between aren't printable.
    local pool
    pool="$(tail -c +16385 "$label" | head -c 114688 | tr -c '[:print:]' '\n' \
        | awk 'prev == "name" && $0 != "" { print; exit } $0 != "" { prev = $0 }')"
    echo "zfs pool='$pool'"
}

# Prints a value from the 'diskutil info' of a device, or nothing.
info() { plutil -extract "$2" raw -o - "$TMP/$1.plist" 2>/dev/null || true; }

scan() {
    local dev="$1" size type mount detected
    diskutil info -plist "$dev" > "$TMP/$dev.plist"
    size="$(info "$dev" TotalSize | awk '{ printf "%.1f GB", $1 / 1000000000 }')"
    type="$(info "$dev" Content)"
    mount="$(info "$dev" MountPoint)"

    local error
    if ! error="$(dd if="/dev/r$dev" of=/dev/null bs=262144 count=1 2>&1)"; then
        warn "can't read /dev/r$dev: $(echo "$error" | head -1)"
        detected="unreadable"
    else
        detected="$(btrfs_info "/dev/r$dev" || zfs_info "/dev/r$dev" || echo "-")"
    fi
    printf '%-10s %10s  %-38s %-22s %s\n' "$dev" "$size" "${type:--}" "${mount:-not mounted}" "$detected"
}

if [ $# -gt 0 ]; then
    disks="$*"
else
    disks="$(diskutil list external physical | awk '/^\/dev\/disk[0-9]+/ { sub("/dev/", "", $1); print $1 }')"
fi
[ -n "$disks" ] || die "no external disks found"

printf '%-10s %10s  %-38s %-22s %s\n' DEVICE SIZE "PARTITION TYPE" "MACOS MOUNT" DETECTED
for disk in $disks; do
    partitions="$(diskutil list "$disk" | awk '{ print $NF }' | grep -E "^${disk}s[0-9]+$" || true)"
    # A disk without a partition table can contain a filesystem itself.
    for dev in ${partitions:-$disk}; do
        scan "$dev"
    done
done
