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
# Runs as root inside the VM (sent by scripts/prepare-vm.sh) after scripts/provision.sh, and seals the image: from the
# next boot on, the disk is attached read-only and the root filesystem is an overlay with a tmpfs on top (the
# 'overlaytmpfs' option of the Alpine initramfs), so everything written while the VM runs is lost when it stops.
#
set -eu

echo "==> Sealing the image"

# The kernel command line is in the Limine configuration.
conf=/boot/limine/limine.conf
sed -i '/^[[:space:]]*cmdline:/{/overlaytmpfs=yes/!s/$/ overlaytmpfs=yes/}' "$conf"
if grep '^[[:space:]]*cmdline:' "$conf" | grep -qv 'overlaytmpfs=yes'; then
    echo "ERROR: can't add overlaytmpfs=yes to $conf" >&2
    exit 1
fi

# The device is read-only: the root filesystem can't be checked at boot, and the EFI partition is mounted read-only
# (the option is appended, since a later 'ro' overrides the 'rw' implied by 'defaults').
awk 'BEGIN { OFS = "\t" }
     /^[[:space:]]*#/ || NF < 6 { print; next }
     $2 == "/"     { $6 = 0 }
     $2 == "/boot" && $4 !~ /(^|,)ro(,|$)/ { $4 = $4 ",ro" }
     { print }' /etc/fstab > /etc/fstab.new
mv /etc/fstab.new /etc/fstab
if ! awk '$2 == "/" && $6 == 0 { root = 1 } $2 == "/boot" && $4 ~ /(^|,)ro$/ { boot = 1 }
          END { exit !(root && boot) }' /etc/fstab; then
    echo "ERROR: unexpected format of /etc/fstab" >&2
    exit 1
fi

# Every boot would look like the first one to cloud-init, since its state is lost: it would, for instance, create new
# ssh host keys each time. The host name, the network configuration and the ssh key it wrote stay in the image.
touch /etc/cloud/cloud-init.disabled

sync
