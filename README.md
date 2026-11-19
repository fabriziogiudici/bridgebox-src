# BridgeBox

BridgeBox gives macOS access to disks with Linux filesystems (btrfs, ZFS) without kernel extensions or FUSE. A small
Alpine Linux VM, run by [vfkit](https://github.com/crc-org/vfkit) on Apple's Virtualization.framework, will own the disk
and expose it to the Mac only, through SMB.


## Requirements

- An Apple Silicon Mac with macOS 14 or later.

- To build BridgeBox from its sources (see [Development](#development)),
  [vfkit](https://github.com/crc-org/vfkit/releases), downloaded by hand: it must be in `PATH`, or its path must be set
  in the `VFKIT` variable (e.g. `make start VFKIT=$HOME/bin/vfkit`). The installer package includes it.

Everything else ships with macOS. The Alpine image is downloaded in the `raw.tar.gz` format, which is already raw as
vfkit requires, so no conversion tool is needed.


## Installation

The installer package, `BridgeBox-<version>.pkg` (built with `make pkg`, see [Installer package](#installer-package)),
installs:

- `/usr/local/bin/BridgeBox`, the command;
- `/usr/local/share/bridgebox/`, owned by root: the sealed VM disk with its ssh key, and vfkit.

The package isn't signed: open it with right click > Open, or install it with `sudo installer -pkg
BridgeBox-<version>.pkg -target /`. Nothing is started at installation. `sudo BridgeBox uninstall` removes everything
(the VM must be stopped).


## Usage

Everything is done with the `BridgeBox` command (from the sources, `bin/BridgeBox`, or the targets of `make`: see
[Development](#development)):

```shell
BridgeBox start [--samba] [disk...]            # starts the VM, mounts the disks and starts the sharing services
BridgeBox stop                                 # unmounts the disks and stops the VM
BridgeBox status                               # state of the VM
BridgeBox ip                                   # IP address of the VM
BridgeBox samba {start | stop | status}        # shares the disks with SMB
BridgeBox disk-status [name]                   # state of the pools and filesystems
BridgeBox trim {start [--wait] | stop} [name]  # TRIM of the pools and filesystems
BridgeBox scrub {start [--wait] | stop} [name] # scrub of the pools and filesystems
BridgeBox speed-test [--mixed]                 # measures the speed of the disks
BridgeBox ssh [command...]                     # logs in to the VM, or runs a command in it
BridgeBox uninstall                            # removes BridgeBox
```

When BridgeBox is installed, the files written while the VM runs (process id, logs, ssh known hosts, EFI variables) are
in `/tmp/bridgebox-<uid>/`.

### Starting and stopping

`start` starts the VM in background and waits until it's reachable with ssh. The disks are given as the identifiers of
their partitions, as `diskutil list` shows them, e.g. `BridgeBox start disk10s2 disk8s2`. A partition must not be
mounted by macOS (unmount it with `diskutil unmount`); if the user can't access it, `sudo` asks for the password to give
it to the user. Disks can't be added to a running VM: `start` with a running VM only starts the sharing services, and
the VM must be stopped first. `--samba` starts Samba, which can also be started later with `samba start`.

The VM first opens the LUKS volumes found on the partitions, asking for their passphrases (three attempts); then it
imports all the ZFS pools found on the partitions and in the LUKS volumes, asks for the passphrase of each encryption
root (three attempts) and mounts the datasets under `/srv/bridgebox` (e.g. `MyVolume/Data` on
`/srv/bridgebox/MyVolume/Data`), without changing the mount points stored in the pools. It also mounts the btrfs
filesystems found on them, also on more than one partition, on `/srv/bridgebox/<label>`, or `/srv/bridgebox/<uuid>` when
the filesystem has no label, the label isn't usable as a share name or another pool or filesystem has the same name; all
the subvolumes are visible as directories. A btrfs filesystem with missing devices isn't mounted.

`stop` stops sharing, unmounts the btrfs filesystems, exports the pools and closes the LUKS volumes; then it stops the
VM, forcing it off if it doesn't react. If the disks can't be unmounted (for instance, when a file is still open, or a
share is still connected from the Finder), the VM is left running: `samba status` shows the open files.

`status` shows the state of the VM as reported by vfkit, and `ip` its IP address. `ssh` logs in to the VM as the user
`bridgebox`, who can run commands as root with `doas`; with a command, it runs it, e.g. `BridgeBox ssh doas getfattr -d
-m - /srv/bridgebox/MyVolume/file`. The VM runs from a read-only image: whatever is written in it, outside the disks, is
lost when it stops.

### Sharing with Samba

Samba isn't started at boot: `samba start` starts it and announces it with Bonjour, until the VM restarts; `samba stop`
removes the announcement and stops Samba, so that the Mac doesn't see the shares anymore (a Mac that is connected loses
them). `samba status` shows whether Samba runs and is announced, and, with `smbstatus`, the connected clients, the
shares in use and the open files (useful when `stop` can't export a pool because a file is still open).

When Samba runs, each mounted pool is shared with SMB as a share with the name of the pool (e.g.
`smb://bridgebox.local/MyVolume`), and each btrfs filesystem as a share with the name of its mount point, so that the
Mac sees the free space of each one; a pool whose root dataset can't be mounted (e.g. its key wasn't loaded) isn't
shared. The shares are read-write, with guest access. Files are accessed as the user `bridgebox` (UID and GID 1100), so
the ones created from the Mac belong to `bridgebox:bridgebox`; files and directories with other owners (e.g. written
when the shares were accessed as root) are only readable from the Mac until they are given to `bridgebox`, e.g. with
`BridgeBox ssh doas chown -R bridgebox:bridgebox /srv/bridgebox/MyVolume`. While Samba runs, the VM is announced with
Bonjour, so `bridgebox` appears in the sidebar of the Finder; otherwise use Go > Connect to Server with
`smb://bridgebox.local` and connect as guest. NetBIOS is disabled. Samba uses `vfs_fruit` and `streams_xattr`, so macOS
metadata (Finder info, tags, extended attributes) are stored as extended attributes of the files on ZFS or btrfs rather
than as AppleDouble `._` files (only resource forks still use `._` files).

### Checking and maintaining the disks

`disk-status` shows, for each imported ZFS pool, `zpool status -v -t -i -s -D` (errors, TRIM, initialization, slow I/O
operations, deduplication), `zpool list -v` and the `autotrim` and `freeing` properties; for each mounted btrfs
filesystem, `btrfs filesystem show`, `btrfs filesystem usage`, `btrfs device stats`, `btrfs scrub status` and `btrfs
balance status`. With a name, it shows only that pool (adding `zfs list -r -o space`, the space used by its datasets),
that dataset (the status of its pool, `zfs list -r -o space` and `zfs get all`) or that btrfs filesystem (by the name of
its mount point), e.g. `disk-status MyVolume/Music`.

`trim start` starts the TRIM of each imported ZFS pool (`zpool trim`), or only of the one given (e.g. `trim start
MyVolume`; a btrfs filesystem is given by the name of its mount point), which runs in background in the VM, or resumes
it if it was suspended; with `--wait`, it waits until it's finished (Ctrl-C stops waiting, not the TRIM). `trim stop`
suspends it (`zpool trim -s`). The TRIM of a pool fails if one of its devices doesn't support it: e.g. a LUKS volume
must be opened with discards (`cryptsetup refresh --allow-discards --persistent <name>` stores the option in the LUKS
header). Mounted btrfs filesystems are trimmed with `fstrim`, which returns only when it's finished: so `trim start`
waits for them even without `--wait`, after starting the ZFS pools, and `trim stop` can't stop them.

`scrub start` starts the scrub of each imported ZFS pool (`zpool scrub`) and of each mounted btrfs filesystem (`btrfs
scrub start`), or only of the one given (e.g. `scrub start MyVolume`), which run in background in the VM; with `--wait`,
it waits until they are finished (Ctrl-C stops waiting, not the scrub). `scrub stop` pauses them (`zpool scrub -p`,
`btrfs scrub cancel`); the next `scrub start` resumes them from where they were: always for ZFS, which keeps the
progress in the pool, but for btrfs only until the VM restarts, since btrfs keeps it in `/var/lib/btrfs`, which is in
RAM. The results are shown by `disk-status`.

### Measuring the speed

`speed-test` measures with `fio`, in turn, the speed of each pool whose root dataset is mounted and of each btrfs
filesystem. By default it measures what a copy (e.g. a backup) does: it writes a test file sequentially, in 1 MiB blocks
through the cache, including the time to flush it to the disk; then, after dropping the caches, it reads back what was
written, through the cache too, as the Finder or a copy does (direct I/O isn't used for reading, since on ZFS it
disables the read-ahead and would measure the latency of the disk rather than its throughput). With `--mixed`, it reads
and writes at the same time, half and half, with direct I/O and 16 requests in flight: this measures the total
throughput of the disk under a mixed load, split between the two directions. Each run lasts at most 30 seconds; at the
end, a summary shows the speeds in MB/s (1 MB = 1,000,000 bytes, as macOS shows them).

The test file is 4 GiB; if there isn't enough free space (with a margin of 256 MiB), 2 GiB and then 1 GiB are tried,
with a warning; if even 1 GiB doesn't fit, an error is reported for that volume and the test goes on with the next one.
The file is written in a temporary directory in the volume, which is always deleted.


## Development

```shell
make image    # downloads Alpine 3.24.4 (cached in cache/), prepares the VM in target/vm, installs btrfs and ZFS
make start    # starts the VM in background and waits for ssh; DISKS="disk10s2 ..." passes partitions to the VM,
              # whose ZFS pools and btrfs filesystems, also in LUKS volumes, are mounted under /srv/bridgebox (the
              # passphrases are asked for); SHARING="--samba" starts Samba
make ssh      # logs in as the 'bridgebox' user
make status   # VM state as reported by vfkit
make ip       # IP address of the VM
make speed-test # measures with fio the write and read speed of each mounted pool and btrfs filesystem
make console  # follows the serial console
make stop     # unmounts the disks and stops the VM: Stop request first, HardStop if the guest doesn't react
make clean    # stops and deletes the VM (the Alpine image is kept; 'make distclean' deletes it too)
```

`make image` runs `scripts/prepare-vm.sh`, which boots the VM once to create the user `bridgebox` and install
`btrfs-progs`, `zfs` and `zfs-virt` (the ZFS kernel modules for the `virt` kernel of the Alpine cloud images), Avahi and
Samba, with `scripts/provision.sh`, and seals the image with `scripts/seal.sh`; then it boots the sealed VM to check
that it runs from a read-only disk, that the kernel modules load, that Samba can be started and stopped and that the Mac
can resolve the VM with Bonjour. This step needs internet access; if it fails, `make image` resumes from the step that
failed. The other targets run `bin/BridgeBox`, which can also be invoked directly, with all the commands of
[Usage](#usage). Settings shared by the two scripts are in `scripts/config.sh`.

Settings can be overridden on the command line, e.g. `make start MEMORY_MB=2048`; see the top of the `Makefile`. The
disk is used as it comes in the Alpine image, without resizing.

### How it works

Once sealed, the image never changes: `bin/BridgeBox` attaches its disk read-only, and the root filesystem of the VM is
an overlay with a tmpfs on top (the `overlaytmpfs` boot option of Alpine), so whatever the VM writes is lost when it
stops. cloud-init is disabled in the sealed image, and the EFI variables are created from scratch at each start in
`/tmp/bridgebox-<uid>/`.

The VM uses the NAT network of Virtualization.framework: it gets an address reachable from the Mac but not from the
local network. Once sealed, the VM announces itself with Bonjour as `bridgebox.local` (IPv4 only), which is how
`bin/BridgeBox` finds its address; while `make image` provisions the VM, before Avahi is installed, the address is taken
from `/var/db/dhcpd_leases`, by the fixed MAC address of the VM.

Mounting is done inside the VM by a back-end, `vm/bridgebox-backend.py` (installed in the image as
`/usr/local/sbin/bridgebox-backend`), which `bin/BridgeBox` runs over ssh. When partitions are passed to `start`, it
first opens the LUKS volumes found on them as `/dev/mapper/bridgebox-<LUKS uuid>`; then it imports all the ZFS pools
found on the partitions and in the LUKS volumes with `zpool import -f -N -R /srv/bridgebox`, loads the keys and mounts
the datasets. It mounts the btrfs filesystems after `btrfs device scan`, so that filesystems on more than one partition
work too; the top-level subvolume is mounted (`subvolid=5`), with the default options of the kernel. `stop` has the
back-end stop sharing, unmount the btrfs filesystems, export the pools and close the LUKS volumes that it opened first,
and doesn't power off the VM if that fails; otherwise the VM gets a Stop request first, and a HardStop if the guest
doesn't react.

Files in `target/vm`:

| File | Content |
|---|---|
| `disk.raw` | the VM disk |
| `efi-vars` | the EFI variable store, used until the image is sealed |
| `id_ed25519` | the development ssh key |
| `cloud-init/` | the cloud-init files passed to the VM (`user-data` includes the ssh public key) |
| `vfkit.log`, `console.log` | vfkit output and serial console |
| `vfkit.pid` | the vfkit process |
| `sealed` | marks the image as sealed |
| `provisioned` | marks the VM as fully prepared |

The REST API socket of vfkit is `/tmp/bridgebox-<uid>/vfkit.sock`, outside `target/vm` because macOS limits the length
of Unix socket paths.

### Installer package

```shell
make pkg                     # or: make pkg VFKIT=$HOME/bin/vfkit VERSION=0.2
```

builds `target/BridgeBox-<version>.pkg` from the prepared VM (`make image` is run first, if needed), with the settings
of `scripts/config.sh` included in the command; see [Installation](#installation) for what it installs.


## License

BridgeBox is licensed under the Apache License, Version 2.0: see [LICENSE.md](LICENSE.md).

---
Copyright 2026 Fabrizio Giudici
