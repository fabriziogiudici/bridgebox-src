#!/usr/bin/env python3
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
"""BridgeBox back-end: runs as root inside the VM, installed as /usr/local/sbin/bridgebox-backend, and is called over
ssh by the BridgeBox command on the Mac.

    bridgebox-backend mount      opens the LUKS volumes found on the disks, asking for their passphrases; imports
                                 all the ZFS pools found on the disks (and in the LUKS volumes) under /srv/bridgebox,
                                 asks for the passphrases of the encrypted ones and mounts their datasets; mounts the
                                 btrfs filesystems under /srv/bridgebox too; shares each pool and each btrfs
                                 filesystem with Samba
    bridgebox-backend unmount    stops sharing, unmounts the btrfs filesystems, exports the ZFS pools (which unmounts
                                 them and unloads their keys) and closes the LUKS volumes
    bridgebox-backend disk-status [name]
                                 shows the status of the ZFS pools and of the btrfs filesystems, with all the
                                 details available; with a name, only of that pool, dataset (with its properties)
                                 or btrfs filesystem
    bridgebox-backend trim {start [--wait] | stop} [name]
                                 starts (or resumes) the TRIM of the ZFS pools, and trims the btrfs filesystems; with
                                 --wait, waits until it's finished; 'stop' suspends the TRIM of the ZFS pools; with a
                                 name, only of that pool or btrfs filesystem
    bridgebox-backend scrub {start [--wait] | stop} [name]
                                 starts (or resumes) the scrub of the ZFS pools and of the btrfs filesystems; with
                                 --wait, waits until it's finished; 'stop' pauses it; with a name, only of that pool
                                 or btrfs filesystem
    bridgebox-backend samba {start | stop | status}
                                 starts or stops Samba and its Bonjour announcement (it isn't started at boot), or
                                 shows whether it runs, with the connected clients and the open files
    bridgebox-backend {zpool | zfs | btrfs} [argument...]
                                 runs zpool, zfs or btrfs with the given arguments, as they are
    bridgebox-backend nfs {start | stop | status}
                                 starts or stops the NFSv4 server (it isn't started at boot), or shows whether it
                                 runs, with the exports and the clients
    bridgebox-backend nfs ids    reads from the standard input the users and groups of the Mac, with which the NFS
                                 server translates the owners that the Mac sends by name ('BridgeBox nfs start' sends
                                 them)
    bridgebox-backend speed-test [--mixed]
                                 measures with fio the speed of each mounted pool and btrfs filesystem, in turn:
                                 sequential writes as a copy does them, then sequential reads; with '--mixed', reads and
                                 writes at the same time, half and half, with direct I/O

Each pool and each btrfs filesystem has its own share, so that the Mac sees its free space: macOS asks for the free
space of the whole share, and /srv/bridgebox itself is in the RAM of the VM.

Pools are imported with -f, since the disks are attached to this VM only: they often were last used by another system
(a different host id), and couldn't be imported otherwise.

A LUKS volume is opened as /dev/mapper/bridgebox-<LUKS uuid>, where 'zpool import' and 'btrfs device scan' find what
it contains; volumes that are already open (for instance, by hand) are left alone, and only the ones opened by the
back-end are closed.

A btrfs filesystem is mounted on /srv/bridgebox/<label>, or /srv/bridgebox/<uuid> when it has no label, or the label
isn't usable as a share name, or another pool or filesystem has the same name. Its top-level subvolume (subvolid=5) is
mounted, so that all the subvolumes are visible as directories; besides that, the default options of the kernel are
used. A filesystem with missing devices isn't mounted.
"""

import errno
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zlib
from xml.sax.saxutils import escape

MOUNT_ROOT = "/srv/bridgebox"
SHARES_CONF = "/etc/samba/bridgebox-shares.conf"   # included by smb.conf
AVAHI_SMB_SERVICE = "/etc/avahi/services/smb.service"   # announces the shares with Bonjour
NFS_EXPORTS = "/etc/ganesha/bridgebox-exports.conf"   # included by ganesha.conf (scripts/provision.sh)
NFS_SERVICE = "bridgebox-nfs"                       # nfs-ganesha, NFSv4 only (written by scripts/provision.sh)
NFS_PORT = 2049
AVAHI_NFS_SERVICES = "/etc/avahi/services/bridgebox-nfs-{}.service"   # announce the NFS exports with Bonjour
NFS_IDS = "/run/bridgebox-nfs"                      # passwd and group of the Mac, for nfs-ganesha
FORWARDED = ("zpool", "zfs", "btrfs")               # run as they are, with the given arguments
PASSPHRASE_ATTEMPTS = 3
SYS_BLOCK = "/sys/class/block"                      # the block devices of the VM, including the device-mapper ones
LUKS_PREFIX = "bridgebox-"                          # of the names of the LUKS volumes opened by the back-end
SCRUB_POLL_SECONDS = 10                             # how often --wait checks the scrub of a btrfs filesystem
SPEED_TEST_SIZES_GIB = (4, 2, 1)                    # sizes of the test file, tried in turn until one fits
SPEED_TEST_MARGIN = 256 * 1024 ** 2                 # bytes that must be left free besides the test file
SPEED_TEST_RUNTIME = 30                             # seconds, at most, of each fio run
SPEED_TEST_FILE = "bridgebox-speed-test"


def log(message):
    print(f"==> {message}", flush=True)


def warn(message):
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def error(message):
    print(f"ERROR: {message}", file=sys.stderr, flush=True)


def output(*command):
    """Runs a command and returns its standard output; raises CalledProcessError if it fails."""
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def importable_pools():
    """Returns (name, id) of the pools that 'zpool import' finds; it fails when there are none, so the exit code is
    ignored. Pools are imported by id, since two pools can have the same name."""
    result = subprocess.run(["zpool", "import"], capture_output=True, text=True)
    pools, name = [], None
    for line in result.stdout.splitlines():
        if match := re.match(r"^\s*pool:\s*(\S+)\s*$", line):
            name = match.group(1)
        elif (match := re.match(r"^\s*id:\s*(\d+)\s*$", line)) and name is not None:
            pools.append((name, match.group(1)))
            name = None
    return pools


def imported_pools():
    return [line for line in output("zpool", "list", "-H", "-o", "name").splitlines() if line]


def datasets(pool, *properties):
    """Returns, for each dataset of the pool, the dictionary of the requested properties (plus 'name')."""
    columns = ("name",) + properties
    lines = output("zfs", "list", "-H", "-r", "-t", "filesystem", "-o", ",".join(columns), pool).splitlines()
    return [dict(zip(columns, line.split("\t"))) for line in lines if line]


def load_keys(pool):
    """Asks for the passphrase of each encryption root of the pool whose key isn't loaded yet."""
    for dataset in datasets(pool, "encryptionroot", "keystatus"):
        if dataset["encryptionroot"] != dataset["name"] or dataset["keystatus"] != "unavailable":
            continue
        for attempt in range(1, PASSPHRASE_ATTEMPTS + 1):
            # zfs reads the passphrase from the terminal, which ssh allocates with -t.
            if subprocess.run(["zfs", "load-key", dataset["name"]]).returncode == 0:
                break
            if attempt < PASSPHRASE_ATTEMPTS:
                warn(f"the key for {dataset['name']} hasn't been loaded, try again")
        else:
            warn(f"the key for {dataset['name']} hasn't been loaded: its datasets won't be mounted")


def mount_datasets(pool):
    """Mounts the datasets of the pool that can be mounted and have their key (or aren't encrypted)."""
    for dataset in datasets(pool, "mountpoint", "canmount", "keystatus", "mounted"):
        if (dataset["canmount"] != "on" or dataset["mountpoint"] in ("none", "legacy", "-")
                or dataset["keystatus"] not in ("available", "-") or dataset["mounted"] == "yes"):
            continue
        result = subprocess.run(["zfs", "mount", dataset["name"]], capture_output=True, text=True)
        if result.returncode == 0:
            log(f"Mounted {dataset['name']} on {dataset['mountpoint']}")
        else:
            warn(f"can't mount {dataset['name']}: {result.stderr.strip()}")


def btrfs_filesystems():
    """Returns the btrfs filesystems on the block devices of the VM, as dictionaries with 'label' (None if it has
    none), 'uuid', 'devices' (their paths) and 'missing' (True if some devices are missing). All the block devices are
    scanned, since there's no udev daemon; they are also registered with the kernel, which needs to know all the
    devices of a filesystem to mount it."""
    subprocess.run(["btrfs", "device", "scan"], capture_output=True, text=True)
    result = subprocess.run(["btrfs", "filesystem", "show", "--all-devices"], capture_output=True, text=True)
    filesystems = []
    for line in result.stdout.splitlines():
        if match := re.match(r"^Label:\s+(?:'(.*)'|none)\s+uuid:\s+(\S+)\s*$", line):
            filesystems.append({"label": match.group(1), "uuid": match.group(2), "devices": [], "missing": False})
        elif filesystems and (match := re.match(r"^\s+devid\s+\d+\s+.*\spath\s+(\S+)\s*$", line)):
            filesystems[-1]["devices"].append(match.group(1))
        elif filesystems and "missing" in line.lower():
            filesystems[-1]["missing"] = True
    return filesystems


def mounts():
    """Returns (device, mount point, filesystem type) for each mounted filesystem."""
    with open("/proc/self/mounts") as file:
        entries = [line.split() for line in file]
    # Spaces and other characters are escaped as octal sequences (e.g. \040) in /proc/self/mounts.
    unescape = lambda field: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)
    return [(unescape(entry[0]), unescape(entry[1]), entry[2]) for entry in entries if len(entry) >= 3]


def btrfs_mounts():
    """Returns the mount points of the btrfs filesystems mounted under MOUNT_ROOT."""
    return [point for _, point, fstype in mounts()
            if fstype == "btrfs" and os.path.dirname(point) == MOUNT_ROOT]


# Characters that Samba doesn't accept in a share name, besides the ones that can't be in a directory name.
SHARE_NAME = re.compile(r"^[^.\s\\/\[\]\":;|<>+=,*?%][^\\/\[\]\":;|<>+=,*?%]{0,79}$")


def mount_btrfs(taken):
    """Mounts the btrfs filesystems that aren't mounted yet under MOUNT_ROOT; 'taken' are the names already in use
    (the ZFS pools). Returns the number of filesystems that couldn't be mounted."""
    failures = 0
    filesystems = btrfs_filesystems()
    if not filesystems:
        log("No btrfs filesystems found")
    mounted = {device: point for device, point, fstype in mounts() if fstype == "btrfs"}
    taken = {name.lower() for name in taken} | {os.path.basename(point).lower() for point in btrfs_mounts()}
    labels = [fs["label"].lower() for fs in filesystems if fs["label"]]
    for fs in filesystems:
        description = f"the btrfs filesystem {fs['label'] or fs['uuid']}"
        points = [mounted[device] for device in fs["devices"] if device in mounted]
        if points:
            if os.path.dirname(points[0]) != MOUNT_ROOT:
                log(f"Ignoring {description}, already mounted on {points[0]}")
            continue
        if fs["missing"] or not fs["devices"]:
            warn(f"{description} isn't mounted, since some of its devices are missing")
            failures += 1
            continue
        label = fs["label"]
        usable = (label and SHARE_NAME.match(label) and label.lower() not in taken
                  and labels.count(label.lower()) == 1 and not os.path.lexists(os.path.join(MOUNT_ROOT, label)))
        name = label if usable else fs["uuid"]
        point = os.path.join(MOUNT_ROOT, name)
        log(f"Mounting {description} on {point}")
        os.makedirs(point, exist_ok=True)
        result = subprocess.run(["mount", "-t", "btrfs", "-o", "subvolid=5", fs["devices"][0], point],
                                capture_output=True, text=True)
        if result.returncode != 0:
            warn(f"can't mount {description}: {(result.stderr or result.stdout).strip()}")
            os.rmdir(point)
            failures += 1
            continue
        taken.add(name.lower())
    return failures


def unmount_btrfs():
    """Unmounts the btrfs filesystems mounted under MOUNT_ROOT; returns the number of the ones that couldn't be."""
    failures = 0
    for point in btrfs_mounts():
        log(f"Unmounting the btrfs filesystem on {point}")
        result = subprocess.run(["umount", point], capture_output=True, text=True)
        if result.returncode != 0:
            warn(f"can't unmount {point} (is it still connected from the Mac? Eject it in the Finder): "
                 f"{(result.stderr or result.stdout).strip()}")
            failures += 1
            continue
        os.rmdir(point)
    return failures


def update_shares(excluded=()):
    """Writes a Samba share for each imported pool whose root dataset is mounted (otherwise the share would be in the
    RAM of the VM) and for each btrfs filesystem mounted under MOUNT_ROOT, except the excluded ones, and has Samba
    reload its configuration. Returns the names of the shares."""
    lines, shared = ["# Written by bridgebox-backend: one share for each mounted volume.", ""], []
    for pool in imported_pools():
        if pool in excluded:
            continue
        root = next((d for d in datasets(pool, "mountpoint", "mounted") if d["name"] == pool), None)
        if root is None or root["mounted"] != "yes":
            warn(f"{pool} isn't shared, since its root dataset isn't mounted")
            continue
        lines += [f"[{pool}]", f"   path = {root['mountpoint']}", ""]
        shared.append(pool)
    for point in btrfs_mounts():
        name = os.path.basename(point)
        if name in excluded:
            continue
        lines += [f"[{name}]", f"   path = {point}", ""]
        shared.append(name)
    with open(SHARES_CONF, "w") as file:
        file.write("\n".join(lines))
    update_exports(excluded)
    # If Samba has been stopped ('samba stop'), it reads the configuration when it's started again.
    if smbd_running():
        result = subprocess.run(["smbcontrol", "smbd", "reload-config"], capture_output=True, text=True)
        if result.returncode != 0:
            warn(f"Samba didn't reload its configuration: {(result.stderr or result.stdout).strip()}")
    return shared


def host_address():
    """Returns the IP address of the Mac as the VM sees it, that is the gateway of the NAT network of Virtualization
    framework; None if there's no default route."""
    match = re.search(r"^default via (\S+)", output("ip", "route", "show", "default"), re.MULTILINE)
    return match.group(1) if match else None


def nfs_running():
    return subprocess.run(["rc-service", NFS_SERVICE, "status"], capture_output=True).returncode == 0


def nfs_path(point):
    """Returns the path with which the Mac reaches a mount point with NFSv4: the Pseudo path of its export in
    nfs-ganesha, relative to MOUNT_ROOT, so /srv/bridgebox/MyVolume is /MyVolume."""
    return "/" + os.path.relpath(point, MOUNT_ROOT)


def nfs_roots(excluded=()):
    """Returns (name, NFSv4 path) of the exports through which the Mac reaches the volumes: the root dataset of each
    imported pool whose root dataset is mounted, and each btrfs filesystem mounted under MOUNT_ROOT, except the
    excluded ones. The child datasets are reachable from them (they are exports nested in them)."""
    roots = []
    for pool in imported_pools():
        if pool in excluded:
            continue
        root = next((d for d in datasets(pool, "mountpoint", "mounted") if d["name"] == pool), None)
        if root is not None and root["mounted"] == "yes":
            roots.append((pool, nfs_path(root["mountpoint"])))
    roots += [(os.path.basename(point), nfs_path(point)) for point in btrfs_mounts()
              if os.path.basename(point) not in excluded]
    return roots


def update_nfs_announcements(roots):
    """Announces with Bonjour (_nfs._tcp, with the path in the TXT record) each of the given exports, as
    '<name> on <host>', and removes the announcements of the other ones; has Avahi read them again if something
    changed."""
    host = socket.gethostname()
    wanted = {}
    for name, path in roots:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        wanted[AVAHI_NFS_SERVICES.format(safe)] = f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Written by bridgebox-backend while the NFS server runs. -->
<service-group>
  <name>{escape(f"{name} on {host}")}</name>
  <service>
    <type>_nfs._tcp</type>
    <port>2049</port>
    <txt-record>path={escape(path)}</txt-record>
  </service>
</service-group>
"""
    changed = False
    for file in glob.glob(AVAHI_NFS_SERVICES.format("*")):
        if file not in wanted:
            os.remove(file)
            changed = True
    for file, content in wanted.items():
        if not os.path.exists(file) or open(file).read() != content:
            with open(file, "w") as stream:
                stream.write(content)
            changed = True
    if changed:
        result = subprocess.run(["avahi-daemon", "--reload"], capture_output=True, text=True)
        if result.returncode != 0:
            warn(f"Avahi didn't read the announcements again: {(result.stderr or result.stdout).strip()}")


def export_id(path, taken):
    """Returns the Export_Id of nfs-ganesha for a path, between 1 and 65534 (0 is the pseudo root), from its CRC32, so
    that it stays the same when the exports are written again; the next free one if it's taken."""
    number = zlib.crc32(path.encode()) % 65534 + 1
    while number in taken:
        number = number % 65534 + 1
    taken.add(number)
    return number


def nfs_pid():
    """Returns the pid of nfs-ganesha, or None if it doesn't run."""
    try:
        pid = int(open("/run/ganesha.pid").read())
        return pid if os.path.isdir(f"/proc/{pid}") else None
    except (OSError, ValueError):
        return None


def update_exports(excluded=()):
    """Writes in NFS_EXPORTS an export of nfs-ganesha for each mounted dataset of the imported pools and for each btrfs
    filesystem mounted under MOUNT_ROOT, except the excluded ones (by the name of the pool or of the filesystem), and
    has nfs-ganesha read them again (SIGHUP) if it runs. They are only for the Mac, read-write, NFSv4 over TCP, with
    the user and group ids that the Mac sends: the files belong to the users of the Mac, as on its disks (e.g. a
    clone), and the ones created from the Mac belong to the user who creates them. Root on the Mac is root here too
    (No_Root_Squash): backup tools such as Carbon Copy Cloner run as root, and must be able to write everywhere; the
    exports are only for the Mac, where root can read the disks anyway. Each one is reached by the Mac with the path
    returned by nfs_path(): those of the child datasets are nested in the one of their pool. The workarounds of
    nfs-ganesha for macOS (named attributes, mounted_on_fileid) are enabled for the Mac: the extended attributes are
    NFSv4 named attributes, stored as user.* extended attributes (VFS named_attr in ganesha.conf)."""
    pools = [pool for pool in imported_pools() if pool not in excluded]
    points = [point for point in btrfs_mounts() if os.path.basename(point) not in excluded]
    lines = ["# Written by bridgebox-backend: one export for each mounted filesystem, only for the Mac."]
    host = host_address()
    if host is None:
        warn("the address of the Mac isn't known (the VM has no default route): nothing is exported with NFS")
    else:
        paths = [dataset["mountpoint"] for pool in pools for dataset in datasets(pool, "mountpoint", "mounted")
                 if dataset["mounted"] == "yes"] + points
        taken = set()
        for path in paths:
            if '"' in path or "\n" in path:
                warn(f"{path} isn't exported with NFS: its name can't be written in the configuration of nfs-ganesha")
                continue
            lines += ["",
                      "EXPORT {",
                      f"\tExport_Id = {export_id(path, taken)};",
                      f'\tPath = "{path}";',
                      f'\tPseudo = "{nfs_path(path)}";',
                      "\tProtocols = 4;",
                      "\tTransports = TCP;",
                      "\tSecType = sys;",
                      "\tSquash = No_Root_Squash;",
                      "\tAccess_Type = None;",
                      "\tFSAL {",
                      "\t\tName = VFS;",
                      "\t}",
                      "\tCLIENT {",
                      f"\t\tClients = {host};",
                      "\t\tProtocols = 4;",
                      "\t\tAccess_Type = RW;",
                      "\t\tmacOS_quirk_named_attr_NVERIFY = true;",
                      "\t\tmacOS_quirk_named_attr_truncate = true;",
                      "\t\tmacOS_quirk_mounted_on_fileid = true;",
                      "\t}",
                      "}"]
    os.makedirs(os.path.dirname(NFS_EXPORTS), exist_ok=True)
    with open(NFS_EXPORTS, "w") as file:
        file.write("\n".join(lines) + "\n")
    # If the NFS server isn't running, it reads the exports when it's started; it's announced only while it runs.
    running = nfs_running()
    pid = nfs_pid() if running else None
    if pid is not None:
        os.kill(pid, signal.SIGHUP)
    update_nfs_announcements(nfs_roots(excluded) if running and host is not None else [])


def nfs_exports():
    """Returns (Pseudo path, path) of the exports written by update_exports()."""
    try:
        text = open(NFS_EXPORTS).read()
    except OSError:
        return []
    return re.findall(r'^\tPath = "([^"]*)";\n\tPseudo = "([^"]*)";', text, re.MULTILINE)


def nfs_clients():
    """Returns the addresses of the clients connected to the NFS server (TCP connections to NFS_PORT)."""
    addresses = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(table).read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            local, remote, state = fields[1], fields[2], fields[3]
            if state != "01" or int(local.rsplit(":", 1)[1], 16) != NFS_PORT:
                continue
            address, port = remote.rsplit(":", 1)
            if len(address) == 8:
                address = socket.inet_ntop(socket.AF_INET, bytes.fromhex(address)[::-1])
            else:
                raw = bytes.fromhex(address)
                address = socket.inet_ntop(socket.AF_INET6, b"".join(raw[k:k + 4][::-1] for k in range(0, 16, 4)))
            addresses.append(f"{address}:{int(port, 16)}")
    return addresses


def read_nfs_ids(kind):
    """Returns the number of the users or of the groups of the Mac (kind: 'passwd' or 'group') written by nfs_ids()."""
    try:
        with open(os.path.join(NFS_IDS, kind)) as file:
            return sum(1 for line in file if line.strip())
    except OSError:
        return 0


def nfs_ids():
    """Reads from the standard input the users and groups of the Mac, as lines 'user <name> <id>' and
    'group <name> <id>' (bin/BridgeBox sends them, from dscl), and writes them in NFS_IDS as passwd and group:
    nfs-ganesha sees them as /etc/passwd and /etc/group (the bridgebox-nfs service binds them in its own mount
    namespace, scripts/provision.sh), and translates with them the owners that the Mac sends by name (e.g.
    fritz@mistral.local, when a copy keeps the owners). The files are rewritten in place, since nfs-ganesha sees them
    through bind mounts. Ids that aren't valid on Linux (e.g. -2, nobody on the Mac) are skipped; the user and the
    group nobody are added as 65534. Returns 1 if nothing has been received."""
    ids = {"user": {}, "group": {}}
    for line in sys.stdin:
        fields = line.split()
        if (len(fields) == 3 and fields[0] in ids and fields[2].isdigit() and int(fields[2]) < 2 ** 32 - 2
                and re.fullmatch(r"[^:@\s]+", fields[1])):
            ids[fields[0]][fields[1]] = int(fields[2])
    if not ids["user"] or not ids["group"]:
        error("the users and groups of the Mac haven't been received")
        return 1
    ids["user"].setdefault("nobody", 65534)
    ids["group"].setdefault("nobody", 65534)
    os.makedirs(NFS_IDS, exist_ok=True)
    with open(os.path.join(NFS_IDS, "passwd"), "w") as file:
        file.writelines(f"{name}:x:{uid}:65534::/var/empty:/sbin/nologin\n"
                        for name, uid in sorted(ids["user"].items()))
    with open(os.path.join(NFS_IDS, "group"), "w") as file:
        file.writelines(f"{name}:x:{gid}:\n" for name, gid in sorted(ids["group"].items()))
    log(f"The NFS server translates the owners with the {len(ids['user'])} users and {len(ids['group'])} groups of "
        "the Mac")
    return 0


def nfs(action):
    """start: writes the exports, starts the NFS server (nfs-ganesha, NFSv4 only) and announces the exports with
    Bonjour; stop: stops it and removes the announcements (a Mac that has mounted an export loses it); status: shows
    whether it runs, the exports, the connected clients and how many users and groups of the Mac are known (see
    nfs_ids()). The server isn't started at boot: start lasts until the VM restarts. Returns 1 if something failed."""
    failures = 0
    if action == "ids":
        return nfs_ids()
    if action == "start":
        update_exports()
        log("Starting the NFS server")
        failures += subprocess.run(["rc-service", NFS_SERVICE, "start"]).returncode != 0
        update_exports()            # now that it runs, also announces the exports with Bonjour
        report_sharing([])
    elif action == "stop":
        log("Stopping the NFS server")
        failures += subprocess.run(["rc-service", NFS_SERVICE, "stop"]).returncode != 0
        update_nfs_announcements([])
    else:
        running = nfs_running() and nfs_pid() is not None
        log(f"The NFS server is {'running' if running else 'stopped'}; the Mac is {host_address() or 'unknown'}")
        if running:
            exports = nfs_exports()
            log("Exports:" + "".join(f"\n    {pseudo}  ({path})" for path, pseudo in exports) if exports
                else "Exports: none")
            log(f"Clients: {', '.join(nfs_clients()) or 'none'}")
            log(f"Users and groups of the Mac known: {read_nfs_ids('passwd')} and {read_nfs_ids('group')}")
    return 1 if failures else 0


def mounted_volumes():
    """Returns (name, mount point) of each imported pool whose root dataset is mounted and of each btrfs filesystem
    mounted under MOUNT_ROOT: the same volumes that are shared."""
    volumes = []
    for pool in imported_pools():
        root = next((d for d in datasets(pool, "mountpoint", "mounted") if d["name"] == pool), None)
        if root is None or root["mounted"] != "yes":
            warn(f"{pool} isn't tested, since its root dataset isn't mounted")
            continue
        volumes.append((pool, root["mountpoint"]))
    return volumes + [(os.path.basename(point), point) for point in btrfs_mounts()]


class SpeedTestError(Exception):
    """A fio run failed; 'full' tells whether the disk got full."""

    def __init__(self, message, full=False):
        super().__init__(message)
        self.full = full


def split_fio_report(text):
    """fio writes the normal report and the JSON one to the same file, the latter in the middle. Returns the first job
    of the JSON report (None if there's none) and the normal report."""
    if text.startswith("{\n"):
        start = 0
    else:
        start = text.find("\n{\n")
        if start < 0:
            return None, text
        start += 1
    try:
        report, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return None, text
    jobs = report.get("jobs") or [None]
    return jobs[0], text[:start] + text[end:].lstrip("\n")


def run_fio(name, directory, *options):
    """Runs a fio job on the test file in the directory, then prints its report (while it runs, fio shows its
    progress). Returns the result of the job, from the JSON report; raises SpeedTestError if it failed."""
    with tempfile.TemporaryDirectory(prefix="bridgebox-fio-") as temporary:
        output = os.path.join(temporary, "report")
        command = ["fio", f"--name={name}", f"--directory={directory}", f"--filename={SPEED_TEST_FILE}", "--bs=1M",
                   "--numjobs=1", f"--runtime={SPEED_TEST_RUNTIME}", "--group_reporting",
                   "--output-format=normal,json", f"--output={output}", *options]
        result = subprocess.run(command, stderr=subprocess.PIPE, text=True)
        text = open(output).read() if os.path.exists(output) else ""
    job, report = split_fio_report(text)
    sys.stdout.write(report)
    sys.stdout.flush()
    sys.stderr.write(result.stderr)
    if result.returncode != 0 or job is None or job.get("error"):
        full = "No space left on device" in result.stderr or (job is not None and job.get("error") == errno.ENOSPC)
        raise SpeedTestError(f"fio failed ({name})", full)
    return job


def sequential_test(directory, size_gib):
    """Writes the test file as a copy does (with buffered I/O, including the time to flush it to the disk), then reads
    back what was written in the same way, as the Finder or a copy does: through the cache, with the read-ahead of
    the filesystem. Direct I/O isn't used for reading, since on ZFS it disables the prefetch and blocks on each
    request, so it would measure the latency of the disk rather than its throughput. The caches are dropped in
    between, so that the file is read from the disk: on ZFS, part of it may still be in the ARC, but the ARC of the
    VM is much smaller than the test file. Returns the speeds, in bytes per second."""
    job = run_fio("seq_write", directory, f"--size={size_gib}G", "--rw=write", "--ioengine=psync", "--end_fsync=1")
    write, written = job["write"]["bw_bytes"], job["write"]["io_bytes"]
    if written == 0:
        raise SpeedTestError("nothing has been written")
    os.sync()
    with open("/proc/sys/vm/drop_caches", "w") as file:
        file.write("3\n")
    # Only what was written is read: the run stops after SPEED_TEST_RUNTIME seconds, and the rest of the file would
    # be read from holes, at a speed that means nothing.
    job = run_fio("seq_read", directory, f"--size={written}", "--rw=read", "--ioengine=psync")
    return {"write": write, "read": job["read"]["bw_bytes"]}


def mixed_test(directory, size_gib):
    """Reads and writes the test file at the same time, half and half, with direct I/O. Returns the speeds, in bytes
    per second."""
    job = run_fio("seq_async", directory, f"--size={size_gib}G", "--rw=readwrite", "--rwmixread=50", "--direct=1",
                  "--ioengine=libaio", "--iodepth=16")
    return {"read": job["read"]["bw_bytes"], "write": job["write"]["bw_bytes"]}


def speed_test_volume(name, point, test):
    """Measures the speed of a volume with the largest test file that fits. Returns the speeds, or the reason why the
    volume hasn't been tested. The test file is in a temporary directory, which is always removed."""
    sizes = list(SPEED_TEST_SIZES_GIB)
    while sizes:
        size = sizes.pop(0)
        stat = os.statvfs(point)
        available = stat.f_bavail * stat.f_frsize
        if available < size * 1024 ** 3 + SPEED_TEST_MARGIN:
            reason = f"there's no space for a {size} GiB test file ({available // 1024 ** 2} MiB available)"
        else:
            log(f"Testing {name} ({point}) with a {size} GiB file")
            directory = tempfile.mkdtemp(prefix=".bridgebox-speed-test-", dir=point)
            try:
                return test(directory, size)
            except SpeedTestError as exception:
                if not exception.full:
                    error(f"the test of {name} failed: {exception}")
                    return "the test failed"
                reason = f"it got full with a {size} GiB test file"
            finally:
                shutil.rmtree(directory, ignore_errors=True)
        if not sizes:
            error(f"{name} hasn't been tested: {reason}")
            return f"not tested: {reason}"
        warn(f"{name}: {reason}, trying with {sizes[0]} GiB")
    return "not tested"


def print_summary(results):
    """Prints the speeds in MB/s (1 MB = 1,000,000 bytes, as macOS shows them), one line for each volume."""
    log("Summary (1 MB = 1,000,000 bytes)")
    width = max(len(name) for name, _ in results)
    for name, result in results:
        if isinstance(result, str):
            line = result
        else:
            line = "   ".join(f"{direction} {speed / 1e6:5.0f} MB/s" for direction, speed in result.items())
        print(f"    {name:<{width}}   {line}", flush=True)


def speed_test(mode="sequential"):
    volumes = mounted_volumes()
    if not volumes:
        error("there are no mounted pools or btrfs filesystems to test: pass their disks to 'BridgeBox start'")
        return 1
    test = mixed_test if mode == "--mixed" else sequential_test
    results = []
    try:
        for name, point in volumes:
            results.append((name, speed_test_volume(name, point, test)))
    except KeyboardInterrupt:
        error("the speed test has been interrupted")
        return 130
    print_summary(results)
    return 1 if any(isinstance(result, str) for _, result in results) else 0


def closed_luks_volumes():
    """Returns (device, uuid, label) of the LUKS volumes on the disks and partitions of the VM that aren't open: an open
    volume has a holder, its device-mapper device. The label is None if the volume has none (LUKS1 has no labels)."""
    volumes = []
    for name in sorted(os.listdir(SYS_BLOCK)):
        holders = os.path.join(SYS_BLOCK, name, "holders")
        if name.startswith(("dm-", "loop", "ram", "zram", "sr")) or (os.path.isdir(holders) and os.listdir(holders)):
            continue
        device = f"/dev/{name}"
        if subprocess.run(["cryptsetup", "isLuks", device], capture_output=True).returncode != 0:
            continue
        uuid = output("cryptsetup", "luksUUID", device).strip()
        dump = subprocess.run(["cryptsetup", "luksDump", device], capture_output=True, text=True).stdout
        match = re.search(r"^Label:\s*(.*?)\s*$", dump, re.MULTILINE)
        label = match.group(1) if match and match.group(1) not in ("", "(no label)") else None
        volumes.append((device, uuid, label))
    return volumes


def open_luks():
    """Opens the LUKS volumes found on the disks, asking for their passphrases; returns the number of the ones that
    couldn't be opened."""
    failures = 0
    for device, uuid, label in closed_luks_volumes():
        description = f"the LUKS volume {label or uuid} ({device})"
        log(f"Opening {description}")
        # cryptsetup reads the passphrase from the terminal, which ssh allocates with -t, and asks again if it's wrong.
        command = ["cryptsetup", "open", f"--tries={PASSPHRASE_ATTEMPTS}", device, LUKS_PREFIX + uuid]
        if subprocess.run(command).returncode != 0:
            warn(f"{description} hasn't been opened: the pools and filesystems in it won't be mounted")
            failures += 1
    return failures


def opened_luks_volumes():
    """Returns the device-mapper names of the LUKS volumes opened by the back-end."""
    names = []
    for name in sorted(os.listdir(SYS_BLOCK)):
        if name.startswith("dm-"):
            with open(os.path.join(SYS_BLOCK, name, "dm", "name")) as file:
                dm_name = file.read().strip()
            if dm_name.startswith(LUKS_PREFIX):
                names.append(dm_name)
    return names


def close_luks():
    """Closes the LUKS volumes opened by the back-end; returns the number of the ones that couldn't be closed (for
    instance, because a pool in them couldn't be exported)."""
    failures = 0
    for name in opened_luks_volumes():
        log(f"Closing the LUKS volume {name}")
        result = subprocess.run(["cryptsetup", "close", name], capture_output=True, text=True)
        if result.returncode != 0:
            warn(f"can't close the LUKS volume {name}: {(result.stderr or result.stdout).strip()}")
            failures += 1
    return failures


def run_shown(*command):
    """Runs a command after printing it; returns its exit status."""
    print(f"$ {' '.join(command)}", flush=True)
    return subprocess.run(command).returncode


def disk_status(target=None):
    """Shows the status of the imported ZFS pools and of the btrfs filesystems mounted under MOUNT_ROOT, with all the
    details available. With target, only of that pool (with the space used by its datasets), dataset (with the
    status of its pool, and its properties) or btrfs filesystem. Returns 1 if a command failed, or if there's nothing
    called target."""
    dataset = None
    if target is None:
        pools, points = imported_pools(), btrfs_mounts()
        if not pools and not points:
            log("There are no imported pools or mounted btrfs filesystems")
            return 0
    elif "/" in target:
        pool = target.split("/", 1)[0]
        exists = subprocess.run(["zfs", "list", "-H", "-o", "name", target], capture_output=True).returncode == 0
        if pool not in imported_pools() or not exists:
            error(f"there's no dataset called {target} in the imported pools")
            return 1
        pools, points, dataset = [pool], [], target
    else:
        volumes = selected_volumes(target)
        if volumes is None:
            return 1
        pools, points = volumes
    failures = 0
    for pool in pools:
        print(flush=True)
        log(f"ZFS pool {pool}")
        # -v: files with errors; -t: TRIM; -i: initialization; -s: slow I/O operations; -D: deduplication table.
        failures += run_shown("zpool", "status", "-v", "-t", "-i", "-s", "-D", pool) != 0
        failures += run_shown("zpool", "list", "-v", pool) != 0
        failures += run_shown("zpool", "get", "autotrim,freeing", pool) != 0
        if target is not None:
            failures += run_shown("zfs", "list", "-r", "-o", "space", dataset or pool) != 0
    if dataset is not None:
        print(flush=True)
        log(f"ZFS dataset {dataset}")
        failures += run_shown("zfs", "get", "all", dataset) != 0
    for point in points:
        print(flush=True)
        log(f"btrfs filesystem {os.path.basename(point)} ({point})")
        for command in (("filesystem", "show"), ("filesystem", "usage"), ("device", "stats"), ("scrub", "status")):
            failures += run_shown("btrfs", *command, point) != 0
        # It exits with 1 when a balance is running or paused, which isn't a failure.
        failures += run_shown("btrfs", "balance", "status", point) not in (0, 1)
    return 1 if failures else 0


def selected_volumes(target):
    """Returns the imported ZFS pools and the mount points of the btrfs filesystems mounted under MOUNT_ROOT: all of
    them, or only the one called target (a btrfs filesystem is called by the name of its mount point). Prints an error
    and returns None if there's none."""
    pools, points = imported_pools(), btrfs_mounts()
    if target is not None:
        pools = [pool for pool in pools if pool == target]
        points = [point for point in points if os.path.basename(point) == target]
        if not pools and not points:
            error(f"there's no imported pool or mounted btrfs filesystem called {target}")
            return None
    elif not pools and not points:
        error("there are no imported pools or mounted btrfs filesystems")
        return None
    return pools, points


def trim(action, wait, target):
    """Starts or resumes (start), or suspends (stop), the TRIM of the imported ZFS pools (or only of target): it runs
    in background in the kernel, and with wait this waits until it's finished. A pool fails if one of its devices
    doesn't support TRIM (e.g. a LUKS volume opened without discards). The btrfs filesystems mounted under MOUNT_ROOT
    are trimmed with fstrim, which returns only when it's finished: so they are trimmed by 'start' even without wait
    (after the ZFS pools have been started), and their TRIM can't be stopped. Returns 1 if something failed."""
    volumes = selected_volumes(target)
    if volumes is None:
        return 1
    pools, points = volumes
    failures = 0
    started = []
    for pool in pools:
        if action == "start":
            log(f"Starting the TRIM of {pool}")
            if subprocess.run(["zpool", "trim", pool]).returncode == 0:
                started.append(pool)
            else:
                failures += 1
        else:
            log(f"Suspending the TRIM of {pool}")
            failures += subprocess.run(["zpool", "trim", "-s", pool]).returncode != 0
    try:
        for point in points:
            name = os.path.basename(point)
            if action == "start":
                log(f"Trimming {name} ({point})")
                failures += subprocess.run(["fstrim", "-v", point]).returncode != 0
            else:
                warn(f"the TRIM of the btrfs filesystem {name} can't be stopped: it runs only during 'trim start'")
        if wait:
            for pool in started:
                log(f"Waiting for the TRIM of {pool}")
                failures += subprocess.run(["zpool", "wait", "-t", "trim", pool]).returncode != 0
    except KeyboardInterrupt:
        error("interrupted: the TRIM of the ZFS pools goes on ('trim stop' suspends it)")
        return 130
    return 1 if failures else 0


def btrfs_scrub_start(point):
    """Resumes the scrub of a btrfs filesystem, if it was stopped since the VM started (btrfs keeps the progress in
    /var/lib/btrfs, which is in RAM), otherwise starts a new one; returns True if it's running. btrfs runs the scrub
    in a child process: it's started in a session of its own, so that it isn't killed when the ssh session ends."""
    result = subprocess.run(["btrfs", "scrub", "resume", point], capture_output=True, text=True,
                            start_new_session=True)
    if result.returncode == 2:      # nothing to resume
        result = subprocess.run(["btrfs", "scrub", "start", point], capture_output=True, text=True,
                                start_new_session=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode == 0


def btrfs_scrub_running(point):
    status = subprocess.run(["btrfs", "scrub", "status", point], capture_output=True, text=True).stdout
    return re.search(r"^Status:\s+running\s*$", status, re.MULTILINE) is not None


def scrub(action, wait, target):
    """Starts or resumes (start), or pauses (stop), the scrub of the imported ZFS pools and of the btrfs filesystems
    mounted under MOUNT_ROOT (or only of target): it runs in background, and with wait this waits until it's
    finished. A paused ZFS scrub is resumed by the next start, even after the VM restarts; a stopped btrfs scrub only
    until the VM restarts. Returns 1 if something failed."""
    volumes = selected_volumes(target)
    if volumes is None:
        return 1
    pools, points = volumes
    failures = 0
    started_pools, started_points = [], []
    for pool in pools:
        if action == "start":
            log(f"Starting the scrub of {pool}")
            if subprocess.run(["zpool", "scrub", pool]).returncode == 0:
                started_pools.append(pool)
            else:
                failures += 1
        else:
            log(f"Pausing the scrub of {pool}")
            failures += subprocess.run(["zpool", "scrub", "-p", pool]).returncode != 0
    for point in points:
        name = os.path.basename(point)
        if action == "start":
            log(f"Starting the scrub of {name} ({point})")
            if btrfs_scrub_start(point):
                started_points.append(point)
            else:
                failures += 1
        else:
            log(f"Stopping the scrub of {name} ({point})")
            failures += subprocess.run(["btrfs", "scrub", "cancel", point]).returncode != 0
    if wait:
        try:
            for pool in started_pools:
                log(f"Waiting for the scrub of {pool}")
                failures += subprocess.run(["zpool", "wait", "-t", "scrub", pool]).returncode != 0
            for point in started_points:
                log(f"Waiting for the scrub of {os.path.basename(point)}")
                while btrfs_scrub_running(point):
                    time.sleep(SCRUB_POLL_SECONDS)
        except KeyboardInterrupt:
            error("interrupted: the scrub goes on ('scrub stop' pauses it)")
            return 130
        log("Finished: 'disk-status' shows the results")
    return 1 if failures else 0


def report_sharing(shares):
    """Logs how the mounted volumes are shared: the Samba shares and the NFS exports are written also when the
    services are stopped, so that they are ready when they are started. With no shares given, only NFS is reported."""
    host = socket.gethostname()
    if shares and smbd_running():
        for name in shares:
            log(f"Shared {name} with SMB as smb://{host}.local/{name}")
    elif shares:
        log(f"Samba is stopped: {', '.join(shares)} will be shared with SMB when it's started ('samba start')")
    roots = nfs_roots()
    if roots and nfs_running():
        for name, path in roots:
            log(f"Exported {name} with NFSv4 as {host}.local:{path}")
    elif roots and shares:
        log(f"The NFS server is stopped: {', '.join(name for name, _ in roots)} will be exported when it's started "
            "('nfs start')")


def smbd_running():
    return subprocess.run(["pidof", "smbd"], capture_output=True).returncode == 0


def samba(action):
    """start: starts Samba and announces it with Bonjour; stop: removes the announcement and stops Samba, so that the
    Mac doesn't see the VM anymore (a Mac that is connected loses the shares); status: shows whether Samba runs and
    is announced, and the connected clients, the shares in use and the open files. Samba isn't started at boot, and
    its announcement is installed disabled (AVAHI_SMB_SERVICE with .off): start lasts until the VM restarts, since the
    file is renamed in the root filesystem, which is in RAM. Returns 1 if something failed."""
    disabled = AVAHI_SMB_SERVICE + ".off"
    failures = 0
    if action == "start":
        log("Starting Samba")
        failures += subprocess.run(["rc-service", "samba", "start"]).returncode != 0
        if os.path.exists(disabled):
            log("Announcing Samba with Bonjour")
            os.rename(disabled, AVAHI_SMB_SERVICE)
            failures += subprocess.run(["avahi-daemon", "--reload"]).returncode != 0
        for name in update_shares():
            log(f"Shared {name} with SMB as smb://{socket.gethostname()}.local/{name}")
    elif action == "stop":
        if os.path.exists(AVAHI_SMB_SERVICE):
            log("Removing the Bonjour announcement of Samba")
            os.rename(AVAHI_SMB_SERVICE, disabled)
            failures += subprocess.run(["avahi-daemon", "--reload"]).returncode != 0
        log("Stopping Samba")
        failures += subprocess.run(["rc-service", "samba", "stop"]).returncode != 0
    else:
        running = smbd_running()
        log(f"Samba is {'running' if running else 'stopped'}, and "
            f"{'announced' if os.path.exists(AVAHI_SMB_SERVICE) else 'not announced'} with Bonjour")
        if running:
            failures += run_shown("smbstatus") != 0
    return 1 if failures else 0


def mount():
    os.makedirs(MOUNT_ROOT, exist_ok=True)
    # The LUKS volumes are opened first, so that the pools and filesystems in them are found.
    failures = open_luks()
    for name, pool_id in importable_pools():
        log(f"Importing the ZFS pool {name}")
        result = subprocess.run(["zpool", "import", "-f", "-N", "-R", MOUNT_ROOT, pool_id], capture_output=True,
                                text=True)
        if result.returncode != 0:
            warn(f"can't import {name}: {result.stderr.strip()}")
            failures += 1

    # Also the pools imported before, for instance when a passphrase was wrong the first time.
    pools = imported_pools()
    if not pools:
        log("No ZFS pools found")
    for name in pools:
        load_keys(name)
        mount_datasets(name)
    failures += mount_btrfs(pools)
    report_sharing(update_shares())
    return 1 if failures else 0


def unmount():
    # The shares are removed first, so that no new connections use the disks; those of the filesystems that can't be
    # unmounted are written again at the end.
    pools = imported_pools()
    update_shares(excluded=pools + [os.path.basename(point) for point in btrfs_mounts()])
    failures = unmount_btrfs()
    for name in pools:
        log(f"Exporting the ZFS pool {name}")
        result = subprocess.run(["zpool", "export", name], capture_output=True, text=True)
        if result.returncode != 0:
            warn(f"can't export {name} (is it still connected from the Mac? Eject it in the Finder): "
                 f"{result.stderr.strip()}")
            failures += 1
    failures += close_luks()
    if failures:
        update_shares()
    return 1 if failures else 0


def parse_start_stop(args):
    """Parses the arguments of the commands that start or stop a background operation on the disks:
    {start [--wait] | stop} [name]. Returns (action, wait, name), with name None if missing; or None if the arguments
    are wrong."""
    if not args or args[0] not in ("start", "stop"):
        return None
    action, rest = args[0], args[1:]
    wait = action == "start" and rest[:1] == ["--wait"]
    if wait:
        rest = rest[1:]
    if len(rest) > 1 or (rest and rest[0].startswith("-")):
        return None
    return action, wait, rest[0] if rest else None


def main():
    commands = {"mount": mount, "unmount": unmount, "disk-status": disk_status, "trim": trim, "scrub": scrub,
                "samba": samba, "nfs": nfs, "speed-test": speed_test}
    valid = (["mount"], ["unmount"], ["samba", "start"], ["samba", "stop"], ["samba", "status"], ["nfs", "start"],
             ["nfs", "stop"], ["nfs", "status"], ["nfs", "ids"], ["speed-test"],
             ["speed-test", "--mixed"])
    command, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else (None, [])
    if command in FORWARDED:
        pass                        # the arguments are passed as they are
    elif command in ("trim", "scrub"):
        args = parse_start_stop(args)
    elif command == "disk-status":
        args = args if len(args) <= 1 and not (args and args[0].startswith("-")) else None
    elif sys.argv[1:] not in valid:
        args = None
    if args is None:
        print(f"Usage: {os.path.basename(sys.argv[0])} {{mount | unmount | disk-status [name]"
              " | trim {start [--wait] | stop} [name] | scrub {start [--wait] | stop} [name]"
              " | samba {start | stop | status} | nfs {start | stop | status}"
              " | speed-test [--mixed]"
              " | {zpool | zfs | btrfs} [argument...]}",
              file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        print("ERROR: must run as root", file=sys.stderr)
        return 1
    if command in FORWARDED:
        # The back-end is replaced by the command: its exit status and its prompts (e.g. 'zfs load-key') are the
        # command's own.
        os.execvp(command, [command, *args])
    return commands[command](*args)


if __name__ == "__main__":
    sys.exit(main())
