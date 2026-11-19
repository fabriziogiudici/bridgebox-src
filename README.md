# BridgeBox

BridgeBox gives macOS access to disks with Linux filesystems (btrfs, ZFS) without kernel extensions or FUSE. A small
Alpine Linux VM, run by [vfkit](https://github.com/crc-org/vfkit) on Apple's Virtualization.framework, will own the disk
and expose it to the Mac only, through ssh and SMB.

## Requirements

- An Apple Silicon Mac with macOS 14 or later.

- [vfkit](https://github.com/crc-org/vfkit/releases), downloaded by hand: it must be in `PATH`, or its path must be set
  in the `VFKIT` variable (e.g. `make start VFKIT=$HOME/bin/vfkit`).

Everything else ships with macOS. The Alpine image is downloaded in the `raw.tar.gz` format, which is already raw as
vfkit requires, so no conversion tool is needed.


## Development VM

```shell
make image    # downloads Alpine 3.24.4 (cached in cache/), prepares the VM in target/vm, installs btrfs and ZFS
make start    # starts the VM in background and waits for ssh; DISKS="disk10s2 ..." passes partitions to the VM,
              # whose ZFS pools and btrfs filesystems, also in LUKS volumes, are mounted under /srv/bridgebox (the
              # passphrases are asked for)
make ssh      # logs in as the 'bridgebox' user
make status   # VM state as reported by vfkit
make ip       # IP address of the VM
make speed-test # measures with fio the write and read speed of each mounted pool and btrfs filesystem;
                # 'make speed-test MODE=mixed' reads and writes at the same time
make console  # follows the serial console
make stop     # unmounts the disks and stops the VM: Stop request first, HardStop if the guest doesn't react
make clean    # stops and deletes the VM (the Alpine image is kept; 'make distclean' deletes it too)
```

`make image` runs `scripts/prepare-vm.sh`, which boots the VM once to create the user `bridgebox` and install
`btrfs-progs`, `zfs` and `zfs-virt` (the ZFS kernel modules for the `virt` kernel of the Alpine cloud images), Avahi
and Samba, with
`scripts/provision.sh`, and seals the image with `scripts/seal.sh`; then it boots the sealed VM to check that it runs
from a read-only disk, that the kernel modules load, that Samba is running and that the Mac can resolve the VM with
Bonjour. This step needs internet access; if it fails, `make image` resumes from the step that failed. The other targets run `bin/BridgeBox`, which can also be invoked directly
(`bin/BridgeBox start [disk...] | stop | status | ip | disk-status [name] | trim {start [--wait] | stop} [name] |
scrub {start [--wait] | stop} [name] | speed-test [mixed] | ssh [command...]`). Settings shared by the two scripts are in
`scripts/config.sh`.

Settings can be overridden on the command line, e.g. `make start MEMORY_MB=2048`; see the top of the `Makefile`. The
disk is used as it comes in the Alpine image, without resizing.

Once sealed, the image never changes: `bin/BridgeBox` attaches its disk read-only, and the root filesystem of the VM
is an overlay with a tmpfs on top (the `overlaytmpfs` boot option of Alpine), so whatever the VM writes is lost when it
stops. cloud-init is disabled in the sealed image, and the EFI variables are created from scratch at each start in
`/tmp/bridgebox-<uid>/`.

The VM uses the NAT network of Virtualization.framework: it gets an address reachable from the Mac but not from the
local network. Once sealed, the VM announces itself with Bonjour as `bridgebox.local` (IPv4 only), which is how
`bin/BridgeBox` finds its address; while `make image` provisions the VM, before Avahi is installed, the address is
taken from `/var/db/dhcpd_leases`, by the fixed MAC address of the VM.

Mounting is done inside the VM by a back-end, `vm/bridgebox-backend.py` (installed in the image as
`/usr/local/sbin/bridgebox-backend`), which `bin/BridgeBox` runs over ssh. When partitions are passed to `start`, it
first opens the LUKS volumes found on them, asking for their passphrases (three attempts), as
`/dev/mapper/bridgebox-<LUKS uuid>`; then it imports all the ZFS pools found on the partitions and in the LUKS volumes
with `zpool import -f -N -R /srv/bridgebox` (so `Clone2/Data` is mounted on
`/srv/bridgebox/Clone2/Data`, without changing the mount points stored in the pool), asks for the passphrase of each
encryption root (three attempts) and mounts the datasets. It also mounts the btrfs filesystems found on them (after
`btrfs device scan`, so that filesystems on more than one partition work too) on `/srv/bridgebox/<label>`, or
`/srv/bridgebox/<uuid>` when the filesystem has no label, the label isn't usable as a share name or another pool or
filesystem has the same name; the top-level subvolume is mounted (`subvolid=5`), so that all the subvolumes are visible
as directories, with the default options of the kernel. A btrfs filesystem with missing devices isn't mounted. `stop`
has the back-end stop sharing, unmount the btrfs filesystems, export the pools and close the LUKS volumes that it opened
first, and doesn't power off the VM if that fails (for instance, when a file is still open, or a share is still connected from the Finder).

`disk-status` shows, for each imported ZFS pool, `zpool status -v -t -i -s -D` (errors, TRIM, initialization, slow I/O
operations, deduplication), `zpool list -v` and the `autotrim` and `freeing` properties; for each mounted btrfs
filesystem, `btrfs filesystem show`, `btrfs filesystem usage`, `btrfs device stats`, `btrfs scrub status` and
`btrfs balance status`. With a name, it shows only that pool (adding `zfs list -r -o space`, the space used by its
datasets), that dataset (the status of its pool, `zfs list -r -o space` and `zfs get all`) or that btrfs filesystem
(by the name of its mount point), e.g. `disk-status Clone2/Music`.

`trim start` starts the TRIM of each imported ZFS pool (`zpool trim`), or only of the one given (e.g. `trim start
Clone2`; a btrfs filesystem is given by the name of its mount point), which runs in background in the VM, or resumes
it if it was suspended; with `--wait`, it waits until it's finished (Ctrl-C stops waiting, not the TRIM). `trim stop`
suspends it (`zpool trim -s`). The TRIM of a pool fails if one of its devices doesn't support it: e.g. a LUKS volume
must be opened with discards (`cryptsetup refresh --allow-discards --persistent <name>` stores the option in the
LUKS header). Mounted btrfs filesystems are trimmed with `fstrim`, which returns only when it's finished: so `trim
start` waits for them even without `--wait`, after starting the ZFS pools, and `trim stop` can't stop them.

`scrub start` starts the scrub of each imported ZFS pool (`zpool scrub`) and of each mounted btrfs filesystem (`btrfs
scrub start`), or only of the one given (e.g. `scrub start Clone2`), which run in background in the VM; with `--wait`, it waits until they are finished (Ctrl-C stops
waiting, not the scrub). `scrub stop` pauses them (`zpool scrub -p`, `btrfs scrub cancel`); the next `scrub start`
resumes them from where they were: always for ZFS, which keeps the progress in the pool, but for btrfs only until the
VM restarts, since btrfs keeps it in `/var/lib/btrfs`, which is in RAM. The results are shown by `disk-status`.

`speed-test` has the back-end measure with `fio`, in turn, the speed of each pool whose root dataset is mounted and of
each btrfs filesystem. By default it measures what a copy (e.g. a backup) does: it writes a test file sequentially, in
1 MiB blocks through the cache, including the time to flush it to the disk; then, after dropping the caches, it reads
back what was written with direct I/O. With `mixed`, it reads and writes at the same time, half and half, with direct
I/O and 16 requests in flight: this measures the total throughput of the disk under a mixed load, split between the two
directions. Each run lasts at most 30 seconds; at the end, a summary shows the speeds in MB/s (1 MB = 1,000,000 bytes,
as macOS shows them).

The test file is 4 GiB; if there isn't enough free space (with a margin of 256 MiB), 2 GiB and then 1 GiB are tried,
with a warning; if even 1 GiB doesn't fit, an error is reported for that volume and the test goes on with the next one.
The file is written in a temporary directory in the volume, which is always deleted.

Each mounted pool is shared with SMB as a share with the name of the pool (e.g. `smb://bridgebox.local/Clone2`), and
each btrfs filesystem as a share with the name of its mount point, so that the Mac sees the free space of each one; a
pool whose root dataset can't be mounted (e.g. its key wasn't loaded) isn't shared. The shares are read-write, with
guest access. Files are accessed as the user `bridgebox` (UID and GID 1100), so the ones created from the Mac belong
to `bridgebox:bridgebox`; files and directories with other owners (e.g. written when the shares were accessed as root)
are only readable from the Mac until they are given to `bridgebox`, e.g. with
`bin/BridgeBox ssh doas chown -R bridgebox:bridgebox /srv/bridgebox/Clone2`. The VM is announced with Bonjour, so
`bridgebox` appears in the sidebar of the Finder; otherwise use Go > Connect to Server with `smb://bridgebox.local` and
connect as guest. NetBIOS is disabled. Samba uses `vfs_fruit` and `streams_xattr`, so macOS metadata (Finder info,
tags, extended attributes) are stored as extended attributes of the files on ZFS or btrfs rather than as AppleDouble
`._` files (only resource forks still use `._` files).

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


## Installer package

```shell
make pkg                     # or: make pkg VFKIT=$HOME/bin/vfkit VERSION=0.2
```

builds `target/BridgeBox-<version>.pkg` from the prepared VM (`make image` is run first, if needed). The package
installs:

- `/usr/local/bin/BridgeBox`, the command, with the settings of `scripts/config.sh` included;
- `/usr/local/share/bridgebox/`, owned by root: the sealed VM disk with its ssh key, and vfkit.

Nothing is started at installation. Once installed, BridgeBox is used with `BridgeBox start [disk...]`,
`BridgeBox stop` and the other commands; the files written while the VM runs (process id, logs, ssh known hosts, EFI
variables) are in `/tmp/bridgebox-<uid>/`. `sudo BridgeBox uninstall` removes everything (the VM must be stopped).

The package isn't signed: open it with right click > Open, or install it with
`sudo installer -pkg target/BridgeBox-<version>.pkg -target /`.


## License

BridgeBox is licensed under the Apache License, Version 2.0: see [LICENSE.md](LICENSE.md).

---
Copyright 2026 Fabrizio Giudici
