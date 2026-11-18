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

import os
import re
import socket
import subprocess
import sys

MOUNT_ROOT = "/srv/bridgebox"
SHARES_CONF = "/etc/samba/bridgebox-shares.conf"   # included by smb.conf
PASSPHRASE_ATTEMPTS = 3
SYS_BLOCK = "/sys/class/block"                      # the block devices of the VM, including the device-mapper ones
LUKS_PREFIX = "bridgebox-"                          # of the names of the LUKS volumes opened by the back-end


def log(message):
    print(f"==> {message}", flush=True)


def warn(message):
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


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
    result = subprocess.run(["smbcontrol", "smbd", "reload-config"], capture_output=True, text=True)
    if result.returncode != 0:
        warn(f"Samba didn't reload its configuration: {(result.stderr or result.stdout).strip()}")
    return shared


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
    for name in update_shares():
        log(f"Shared {name} as smb://{socket.gethostname()}.local/{name}")
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


def main():
    commands = {"mount": mount, "unmount": unmount}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(f"Usage: {os.path.basename(sys.argv[0])} {{mount | unmount}}", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        print("ERROR: must run as root", file=sys.stderr)
        return 1
    return commands[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
