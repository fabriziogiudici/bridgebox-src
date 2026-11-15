#!/bin/sh
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
# Runs as root inside the VM (sent by scripts/prepare-vm.sh) and installs the btrfs and ZFS software.
# It can be run more than once.
#
set -eu

echo "==> Upgrading the system"
apk update
apk upgrade

# zfs-virt contains the ZFS kernel modules for the 'virt' kernel of the Alpine cloud images; it requires the latest
# linux-virt, so the kernel may be upgraded too, and the modules can be loaded only after a reboot.
# The btrfs kernel module is already part of linux-virt.
echo "==> Installing btrfs and ZFS"
apk add btrfs-progs zfs zfs-virt

# lsblk lists the block devices of the VM (the disks passed by 'BridgeBox start'), with their sizes and filesystems.
echo "==> Installing lsblk"
apk add lsblk

# The back-end (installed by scripts/prepare-vm.sh) is written in Python. cloud-init already needs it, but the
# dependency is made explicit.
echo "==> Installing Python"
apk add python3

sync
