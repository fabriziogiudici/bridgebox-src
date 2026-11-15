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

    bridgebox-backend mount      imports all the ZFS pools found on the disks under /srv/bridgebox, asks for the
                                 passphrases of the encrypted ones and mounts their datasets; mounts the btrfs
                                 filesystems under /srv/bridgebox too
    bridgebox-backend unmount    unmounts the btrfs filesystems and exports the ZFS pools (which unmounts them and
                                 unloads their keys)

Pools are imported with -f, since the disks are attached to this VM only: they often were last used by another system
(a different host id), and couldn't be imported otherwise.

A btrfs filesystem is mounted on /srv/bridgebox/<label>, or /srv/bridgebox/<uuid> when it has no label, or the label
isn't usable as a directory name, or another pool or filesystem has the same name. Its top-level subvolume
(subvolid=5) is mounted, so that all the subvolumes are visible as directories; besides that, the default options of
the kernel are used. A filesystem with missing devices isn't mounted.
"""

import os
import re
import subprocess
import sys

MOUNT_ROOT = "/srv/bridgebox"
PASSPHRASE_ATTEMPTS = 3


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


# A label is used as the name of the mount point only if it's a plain directory name, not hidden.
DIRECTORY_NAME = re.compile(r"^[^./][^/]*$")


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
        usable = (label and DIRECTORY_NAME.match(label) and label.lower() not in taken
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
            warn(f"can't unmount {point}: {(result.stderr or result.stdout).strip()}")
            failures += 1
            continue
        os.rmdir(point)
    return failures


def mount():
    os.makedirs(MOUNT_ROOT, exist_ok=True)
    failures = 0
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
    return 1 if failures else 0


def unmount():
    failures = unmount_btrfs()
    for name in imported_pools():
        log(f"Exporting the ZFS pool {name}")
        result = subprocess.run(["zpool", "export", name], capture_output=True, text=True)
        if result.returncode != 0:
            warn(f"can't export {name}: {result.stderr.strip()}")
            failures += 1
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
