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
# Runs as root inside the VM (sent by scripts/prepare-vm.sh): creates the user bridgebox, installs the btrfs and ZFS
# software, Avahi to announce the VM with Bonjour, and Samba to share it with the Mac. It can be run more than once.
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

# Avahi announces the host name of the VM (set by cloud-init) as <name>.local, so the Mac can find the VM by name
# instead of looking for its DHCP lease. Only IPv4 is published: an IPv6 link-local address would be less practical
# for ssh and SMB. The announcements stay on the NAT network of the VM, which isn't bridged to the local network.
echo "==> Installing Avahi"
apk add avahi dbus
conf=/etc/avahi/avahi-daemon.conf
sed -i -e 's/^#*use-ipv6=.*/use-ipv6=no/' -e 's/^#*publish-aaaa-on-ipv4=.*/publish-aaaa-on-ipv4=no/' "$conf"
if ! grep -q '^use-ipv6=no$' "$conf" || ! grep -q '^publish-aaaa-on-ipv4=no$' "$conf"; then
    echo "ERROR: unexpected format of $conf" >&2
    exit 1
fi
for service in dbus avahi-daemon; do
    [ -e "/etc/runlevels/default/$service" ] || rc-update add "$service" default
done

# The user bridgebox: bin/BridgeBox logs in as it, and Samba accesses the shared files as it. It's created here, rather
# than by cloud-init, as a copy of the default user 'alpine' (which is kept, and is used by scripts/prepare-vm.sh to
# run this script): the same password field (locked: only the ssh key works), so that sshd treats the two users the
# same way, the same authorized ssh key, and doas without password. The UID and GID are fixed, since the files on the
# disks keep the numeric owner.
BRIDGEBOX_ID=1100
echo "==> Creating the user bridgebox"
grep -q '^bridgebox:' /etc/group || addgroup -g "$BRIDGEBOX_ID" bridgebox
id bridgebox > /dev/null 2>&1 || adduser -D -u "$BRIDGEBOX_ID" -G bridgebox -g BridgeBox -s /bin/ash bridgebox
if [ "$(id -u bridgebox)" != "$BRIDGEBOX_ID" ] || [ "$(id -g bridgebox)" != "$BRIDGEBOX_ID" ]; then
    echo "ERROR: the user bridgebox exists, but its UID or GID isn't $BRIDGEBOX_ID" >&2
    exit 1
fi
password="$(awk -F: '$1 == "alpine" { print $2 }' /etc/shadow)"
if [ -z "$password" ]; then
    echo "ERROR: the user alpine has no password field in /etc/shadow" >&2
    exit 1
fi
(
    umask 077
    awk -F: -v OFS=: -v password="$password" '$1 == "bridgebox" { $2 = password } { print }' /etc/shadow \
        > /etc/shadow.new
)
cat /etc/shadow.new > /etc/shadow   # keeps the owner and the mode of /etc/shadow
rm /etc/shadow.new
alpine_home="$(awk -F: '$1 == "alpine" { print $6 }' /etc/passwd)"
install -d -m 700 -o bridgebox -g bridgebox /home/bridgebox/.ssh
install -m 600 -o bridgebox -g bridgebox "$alpine_home/.ssh/authorized_keys" /home/bridgebox/.ssh/authorized_keys
# The doas of Alpine reads only /etc/doas.d/*.conf when that directory exists, otherwise /etc/doas.conf (where
# cloud-init writes the rule of 'alpine'): the rule goes where it's read, and is checked.
rule="permit nopass bridgebox"
if [ -d /etc/doas.d ]; then
    echo "$rule" > /etc/doas.d/bridgebox.conf
    chmod 600 /etc/doas.d/bridgebox.conf
elif ! grep -qx "$rule" /etc/doas.conf; then
    echo "$rule" >> /etc/doas.conf
fi
if ! su -s /bin/sh bridgebox -c 'doas -n true'; then
    echo "ERROR: the user bridgebox can't use doas" >&2
    exit 1
fi

# Samba shares each ZFS pool and each btrfs filesystem mounted by the back-end under /srv/bridgebox as a share of its
# own, so that the Mac sees the real free space of each one. The shares are written by the back-end in
# bridgebox-shares.conf, which is empty in the image. They are read-write, to guests: they are reachable only from the
# Mac. Files are accessed as the user bridgebox (force user and force group), so the ones created from the Mac belong
# to bridgebox:bridgebox; files and directories with other owners are only readable, unless they are made bridgebox's
# (chown). NetBIOS is disabled (nmbd isn't started, no port 139): the Mac finds the VM with Bonjour and connects on
# port 445.
echo "==> Installing Samba"
apk add samba
mkdir -p /srv/bridgebox

# iotop can be used for profiling
echo "==> Installing iotop"
apk add iotop

# cryptsetup can be used to mount VeraCrypt volumes
echo "==> Installing cryptsetup"
apk add cryptsetup

# fio is used by the back-end to measure the speed of the disks ('BridgeBox speed-test').
echo "==> Installing fio"
apk add fio

# rsync rclone can be useful for maintenance
echo "==> Installing rsync and rclone"
apk add rsync rclone

# Samba must be able to resolve the host name of the VM: otherwise the SMB negotiation waits for a DNS timeout and then
# fails (NT_STATUS_INVALID_PARAMETER). cloud-init sets the host name, but doesn't add it to /etc/hosts.
name="$(hostname)"
if ! awk -v name="$name" '!/^[[:space:]]*#/ { for (i = 2; i <= NF; i++) if ($i == name) found = 1 }
                          END { exit !found }' /etc/hosts; then
    echo "127.0.1.1	$name" >> /etc/hosts
fi

cat > /etc/samba/smb.conf << 'EOF'
# Written by scripts/provision.sh.
[global]
   server role = standalone server
   server string = BridgeBox
   disable netbios = yes
   smb ports = 445
   server min protocol = SMB2
   map to guest = Bad User
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes
   log file = /var/log/samba/%m.log
   max log size = 50
   # Defaults for the shares written by the back-end.
   read only = no
   guest ok = yes
   guest only = yes
   force user = bridgebox
   force group = bridgebox
   # macOS metadata (Finder info, tags, extended attributes) are stored as extended attributes of the files,
   # instead of AppleDouble ._ files.
   vfs objects = catia fruit streams_xattr
   fruit:metadata = stream
   fruit:model = MacSamba
   fruit:posix_rename = yes
   fruit:veto_appledouble = no
   fruit:nfs_aces = no
   fruit:wipe_intentionally_left_blank_rfork = yes
   fruit:delete_empty_adfiles = yes

include = /etc/samba/bridgebox-shares.conf
EOF
echo "# Written by bridgebox-backend: one share for each mounted volume." > /etc/samba/bridgebox-shares.conf
conf=/etc/conf.d/samba
sed -i 's/^daemon_list=.*/daemon_list="smbd"/' "$conf"
if ! grep -q '^daemon_list="smbd"$' "$conf"; then
    echo "ERROR: unexpected format of $conf" >&2
    exit 1
fi
# Samba isn't started at boot: 'BridgeBox samba start' (or 'BridgeBox start --samba') starts it.
[ ! -e /etc/runlevels/default/samba ] || rc-update del samba default

# The Bonjour announcement of the SMB shares, which makes the VM appear in the sidebar of the Finder, is installed
# disabled (.off): the back-end enables it when it starts Samba.
rm -f /etc/avahi/services/smb.service
cat > /etc/avahi/services/smb.service.off << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_smb._tcp</type>
    <port>445</port>
  </service>
</service-group>
EOF

sync
