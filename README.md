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
              # whose ZFS pools and btrfs filesystems are mounted under /srv/bridgebox (the passphrases are asked for)
make ssh      # logs in as the 'alpine' user
make status   # VM state as reported by vfkit
make ip       # IP address of the VM
make console  # follows the serial console
make stop     # unmounts the disks and stops the VM: Stop request first, HardStop if the guest doesn't react
make clean    # stops and deletes the VM (the Alpine image is kept; 'make distclean' deletes it too)
```

`make image` runs `scripts/prepare-vm.sh`, which boots the VM once to install `btrfs-progs`, `zfs` and `zfs-virt`
(the ZFS kernel modules for the `virt` kernel of the Alpine cloud images), Avahi and Samba, with
`scripts/provision.sh`, and seals the image with `scripts/seal.sh`; then it boots the sealed VM to check that it runs
from a read-only disk, that the kernel modules load, that Samba is running and that the Mac can resolve the VM with
Bonjour. This step needs internet access; if it fails, `make image` resumes from the step that failed. The other targets run `bin/BridgeBox`, which can also be invoked directly
(`bin/BridgeBox start [disk...] | stop | status | ip | ssh [command...]`). Settings shared by the two scripts are in
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
imports all the ZFS pools found on them with `zpool import -f -N -R /srv/bridgebox` (so `Clone2/Data` is mounted on
`/srv/bridgebox/Clone2/Data`, without changing the mount points stored in the pool), asks for the passphrase of each
encryption root (three attempts) and mounts the datasets. It also mounts the btrfs filesystems found on them (after
`btrfs device scan`, so that filesystems on more than one partition work too) on `/srv/bridgebox/<label>`, or
`/srv/bridgebox/<uuid>` when the filesystem has no label, the label isn't usable as a share name or another pool or
filesystem has the same name; the top-level subvolume is mounted (`subvolid=5`), so that all the subvolumes are visible
as directories, with the default options of the kernel. A btrfs filesystem with missing devices isn't mounted. `stop`
has the back-end stop sharing, unmount the btrfs filesystems and export the pools first, and doesn't power off the VM
if that fails (for instance, when a file is still open, or a share is still connected from the Finder).

Each mounted pool is shared with SMB as a share with the name of the pool (e.g. `smb://bridgebox.local/Clone2`), and
each btrfs filesystem as a share with the name of its mount point, so that the Mac sees the free space of each one; a
pool whose root dataset can't be mounted (e.g. its key wasn't loaded) isn't shared. The shares are read-write, with
guest access (files are accessed as root). The VM is announced with Bonjour, so `bridgebox` appears in the sidebar of
the Finder; otherwise use Go > Connect to Server with `smb://bridgebox.local` and connect as guest. NetBIOS is
disabled. Samba uses `vfs_fruit` and `streams_xattr`, so macOS metadata (Finder info, tags, extended attributes) are
stored as extended attributes of the files on ZFS or btrfs rather than as AppleDouble `._` files (only resource forks
still use `._` files).

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
