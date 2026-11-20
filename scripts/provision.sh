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
# software, Avahi to announce the VM with Bonjour, and Samba and the NFS server to share it with the Mac. It can be run
# more than once.
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

# The NFS server is nfs-ganesha, built from its sources: the NFS server of the kernel can't store the extended
# attributes of the Mac, which macOS sends over NFSv4.0 as named attributes (OPENATTR); nfs-ganesha supports them since
# V15.10, and stores each one as a "user." extended attribute (VFS named_attr), with workarounds for the NFS client of
# macOS (USE_MACOS_QUIRKS, off by default). It isn't packaged by Alpine. It's built in a tmpfs, and the build tools are
# removed afterwards (its CMake files require OpenSSL even without TLS, which isn't built: it isn't linked). Changes to
# the sources, for musl and for BridgeBox:
# - the CMake files define _GNU_SOURCE only with glibc: it's defined for musl too, which then declares the same GNU
#   functions (e.g. copy_file_range);
# - musl lacks some glibc extensions (malloc_trim, adaptive mutexes, RTLD_DEEPBIND, S_BLKSIZE, innetgr, execinfo.h,
#   mcheck.h): a header force-included in every source file, and two empty headers, provide them;
# - some generated headers redefine int32_t, uint32_t, int64_t and uint64_t, with types that differ with musl: those
#   typedefs are removed (the ones of stdint.h are used);
# - the names of the owners that the Mac sends are user@domain, where the domain is the one of the Mac (e.g.
#   fritz@mistral.local): nfs-ganesha translates only the names of its own domain, so the check of the domain is
#   removed, and the name before '@' is always used;
# - the default stack of the threads of musl is 128 KiB: the one of glibc, 8 MiB, is set at link time.
# Only the VFS FSAL and NFSv4 are built (no NFSv3, NLM, RQUOTA, rpcbind, DBus, Kerberos).
GANESHA_VERSION=V15.12
GANESHA_COMMIT=f7a6811d936f776261bce0fd08570e214fe0b061
echo "==> Installing nfs-ganesha $GANESHA_VERSION"
apk add userspace-rcu acl-libs libblkid libuuid libcap2
if [ ! -x /usr/bin/ganesha.nfsd ]; then
    apk add --virtual .ganesha-build build-base cmake git bison flex linux-headers bsd-compat-headers \
        userspace-rcu-dev acl-dev util-linux-dev libcap-dev openssl-dev
    build=/tmp/ganesha-build
    mkdir -p "$build"
    grep -q " $build tmpfs " /proc/mounts || mount -t tmpfs -o size=1200m tmpfs "$build"
    rm -rf "${build:?}"/*
    git clone --quiet --depth 1 --branch "$GANESHA_VERSION" --recurse-submodules --shallow-submodules \
        https://github.com/nfs-ganesha/nfs-ganesha.git "$build/nfs-ganesha"
    if [ "$(git -C "$build/nfs-ganesha" rev-parse HEAD)" != "$GANESHA_COMMIT" ]; then
        echo "ERROR: nfs-ganesha $GANESHA_VERSION isn't the expected commit $GANESHA_COMMIT" >&2
        exit 1
    fi
    src="$build/nfs-ganesha/src"
    mkdir -p "$build/musl/bits"
    cat > "$build/musl/musl-compat.h" << 'EOF'
/* glibc extensions used by nfs-ganesha and libntirpc, missing in musl (force-included in every source file). */
#ifndef BRIDGEBOX_MUSL_COMPAT_H
#define BRIDGEBOX_MUSL_COMPAT_H
#if !defined(__GLIBC__) && defined(__linux__) && !defined(__cplusplus)
#include <sys/cdefs.h>                  /* glibc includes it everywhere: __BEGIN_DECLS, __END_DECLS */
#include <stddef.h>
static inline int malloc_trim(size_t pad) { (void)pad; return 0; }
#define PTHREAD_MUTEX_ADAPTIVE_NP 0     /* PTHREAD_MUTEX_NORMAL */
#define PTHREAD_ADAPTIVE_MUTEX_INITIALIZER_NP PTHREAD_MUTEX_INITIALIZER
#define RTLD_DEEPBIND 0                 /* dlopen flag, glibc only */
#define S_BLKSIZE 512                   /* the unit of st_blocks */
/* No netgroups in musl: no host is in a netgroup (BridgeBox doesn't use them). */
static inline int innetgr(const char *netgroup, const char *host, const char *user, const char *domain)
{ (void)netgroup; (void)host; (void)user; (void)domain; return 0; }
#endif
#endif
EOF
    cat > "$build/musl/execinfo.h" << 'EOF'
/* musl has no execinfo: backtraces are empty (used only for logging crashes). */
#ifndef BRIDGEBOX_EXECINFO_H
#define BRIDGEBOX_EXECINFO_H
static inline int backtrace(void **buffer, int size) { (void)buffer; (void)size; return 0; }
static inline char **backtrace_symbols(void *const *buffer, int size) { (void)buffer; (void)size; return 0; }
static inline void backtrace_symbols_fd(void *const *buffer, int size, int fd) { (void)buffer; (void)size; (void)fd; }
#endif
EOF
    cat > "$build/musl/mcheck.h" << 'EOF'
/* musl has no mcheck: memory tracing (only reachable from DBus admin calls) does nothing. */
#ifndef BRIDGEBOX_MCHECK_H
#define BRIDGEBOX_MCHECK_H
static inline void mtrace(void) {}
static inline void muntrace(void) {}
#endif
EOF
    echo '#include <endian.h>' > "$build/musl/bits/endian.h"
    typedefs='^typedef (int|u_int|quad_t|u_quad_t) (int32_t|uint32_t|int64_t|uint64_t);$'
    grep -lE "$typedefs" "$src"/include/*.h | while read -r header; do
        sed -i -E "/$typedefs/d" "$header"
    done
    check='strcmp(at + 1, owner_domain.domain.addr) != 0'
    sed -i "s|if ($check) {|if (0 \\&\\& $check) {|" "$src/idmapper/idmapper.c"
    if ! grep -qF "if (0 && $check) {" "$src/idmapper/idmapper.c" \
            || grep -qE "$typedefs" "$src"/include/*.h; then
        echo "ERROR: the sources of nfs-ganesha couldn't be changed" >&2
        exit 1
    fi
    mkdir -p "$build/build"
    (cd "$build/build" && cmake "$src" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr -DSYSCONFDIR=/etc \
        -DCMAKE_INSTALL_LIBDIR=lib -DLIB_INSTALL_DIR=/usr/lib -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_C_FLAGS="-D_GNU_SOURCE=1 -I$build/musl -include $build/musl/musl-compat.h" \
        -DCMAKE_EXE_LINKER_FLAGS="-Wl,-z,stack-size=8388608" \
        -DUSE_MACOS_QUIRKS=ON -DUSE_ACL_MAPPING=ON -DUSE_NFSIDMAP=OFF -DUSE_GSS=OFF -D_MSPAC_SUPPORT=OFF \
        -DUSE_NFS3=OFF -DUSE_NLM=OFF -DUSE_RQUOTA=OFF -DUSE_NFSACL3=OFF -DRPCBIND=OFF -DUSE_9P=OFF \
        -DUSE_DBUS=OFF -DUSE_MONITORING=OFF -DLEGACY_METRICS=OFF -DUSE_RADOS_RECOV=OFF -DRADOS_URLS=OFF \
        -DCEPHFS_POSIX_ACL=OFF -DUSE_ADMIN_TOOLS=OFF -DUSE_GUI_ADMIN_TOOLS=OFF -DUSE_MAN_PAGE=OFF \
        -DUSE_FSAL_PROXY_V4=OFF -DUSE_FSAL_PROXY_V3=OFF -DUSE_FSAL_LUSTRE=OFF -DUSE_FSAL_KVSFS=OFF \
        -DUSE_FSAL_CEPH=OFF -DUSE_FSAL_GPFS=OFF -DUSE_FSAL_XFS=OFF -DUSE_FSAL_GLUSTER=OFF -DUSE_FSAL_NULL=OFF \
        -DUSE_FSAL_RGW=OFF -DUSE_FSAL_MEM=OFF -DUSE_FSAL_SAUNAFS=OFF -DUSE_FSAL_LIZARDFS=OFF \
        && make -j2 && make install)
    umount "$build"
    rmdir "$build"
    apk del .ganesha-build
fi
# The libraries are in /usr/lib, where musl looks for them (not in /usr/lib64, the default of CMake).
if [ ! -x /usr/bin/ganesha.nfsd ] || [ ! -e /usr/lib/ganesha/libfsalvfs.so ] \
        || ldd /usr/bin/ganesha.nfsd /usr/lib/ganesha/libfsalvfs.so 2>&1 | grep -q "not found"; then
    echo "ERROR: nfs-ganesha isn't installed correctly" >&2
    exit 1
fi
[ ! -e /etc/runlevels/default/bridgebox-nfs ] || rc-update del bridgebox-nfs default
# The configuration of nfs-ganesha: NFSv4 only, without grace period (the state of the clients isn't kept across
# restarts anyway); the owners are sent to the Mac as numbers, which it understands without any translation; the
# workarounds for macOS apply to all the clients (they are only the Mac, which uses NFSv4.0, while nfs-ganesha
# recognizes macOS only from NFSv4.1). The back-end writes the exports in /etc/ganesha/bridgebox-exports.conf.
mkdir -p /etc/ganesha
cat > /etc/ganesha/ganesha.conf << 'EOF'
# Written by scripts/provision.sh: the NFS server of BridgeBox (nfs-ganesha).
NFS_CORE_PARAM {
	Protocols = 4;
	NFS_Port = 2049;
	allow_set_io_flusher_fail = true;
}
NFSV4 {
	Graceless = true;
	Only_Numeric_Owners = true;
	macOS_quirks_only_for_macOS_clients = false;
	macOS_quirk_pad_handles = true;
}
DIRECTORY_SERVICES {
	DomainName = "bridgebox";
}
VFS {
	named_attr = true;
}
LOG {
	Default_Log_Level = EVENT;
}
EXPORT_DEFAULTS {
	Access_Type = None;
}
%include "/etc/ganesha/bridgebox-exports.conf"
EOF
echo "# Written by bridgebox-backend: one export for each mounted filesystem, only for the Mac." \
    > /etc/ganesha/bridgebox-exports.conf
# When the Mac sets the owner of a file, NFSv4 sends it as a name, user@domain, which nfs-ganesha translates into an id
# with getpwnam() and getgrnam(). It must do so with the users and groups of the Mac, whose names and ids differ from
# the ones of the VM (e.g. staff is 20 on the Mac): so it runs in a mount namespace of its own (a slave of the one of
# the VM, so that it sees the filesystems mounted later), where
# /run/bridgebox-nfs/passwd and group, written by the back-end with the users and groups of the Mac ('BridgeBox nfs
# start' sends them), replace /etc/passwd and /etc/group.
cat > /etc/init.d/bridgebox-nfs << 'EOF'
#!/sbin/openrc-run
# The NFS server of BridgeBox, nfs-ganesha (written by scripts/provision.sh).
description="NFSv4 server of BridgeBox (nfs-ganesha)"
extra_started_commands="reload"

depend() {
    need net
}

start() {
    ebegin "Starting the NFS server"
    mkdir -p /var/lib/nfs/ganesha /run/bridgebox-nfs
    # Until the back-end writes the users and groups of the Mac, only root, wheel and nobody are known.
    [ -e /run/bridgebox-nfs/passwd ] || printf '%s\n' "root:x:0:0::/var/empty:/sbin/nologin" \
        "nobody:x:65534:65534::/var/empty:/sbin/nologin" > /run/bridgebox-nfs/passwd
    [ -e /run/bridgebox-nfs/group ] || printf '%s\n' "wheel:x:0:" "nobody:x:65534:" > /run/bridgebox-nfs/group
    # nfs-ganesha must see the filesystems that the back-end mounts after it's started: its mount namespace is a slave
    # of the one of the VM, whose mounts are made shared (with OpenRC they are private), so that they propagate into it,
    # while its own bind mounts don't propagate out.
    mount --make-rshared /
    unshare --mount --propagation slave sh -c 'mount --bind /run/bridgebox-nfs/passwd /etc/passwd \
        && mount --bind /run/bridgebox-nfs/group /etc/group \
        && exec /usr/bin/ganesha.nfsd -f /etc/ganesha/ganesha.conf -L SYSLOG -p /run/ganesha.pid'
    eend $?
}

stop() {
    ebegin "Stopping the NFS server"
    start-stop-daemon --stop --pidfile /run/ganesha.pid --retry 10
    eend $?
}

reload() {
    ebegin "Reloading the exports of the NFS server"
    start-stop-daemon --signal HUP --pidfile /run/ganesha.pid
    eend $?
}
EOF
chmod 755 /etc/init.d/bridgebox-nfs

sync
