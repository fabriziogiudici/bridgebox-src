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
make image    # downloads Alpine 3.24.4 (cached in cache/) and prepares the VM in target/vm
make start    # starts the VM in background and waits for ssh
make ssh      # logs in as the 'alpine' user
make status   # VM state as reported by vfkit
make ip       # IP address of the VM
make console  # follows the serial console
make stop     # stops the VM: Stop request first, HardStop if the guest doesn't react
make clean    # stops and deletes the VM (the Alpine image is kept; 'make distclean' deletes it too)
```

`make image` runs `scripts/prepare-vm.sh`, which boots the VM once to seal the image with `scripts/seal.sh`; then it
boots the sealed VM to check that it runs from a read-only disk. If a step fails, `make image` resumes from it. The other targets run `bin/BridgeBox`, which can also be invoked directly
(`bin/BridgeBox start | stop | status | ip | ssh [command...]`). Settings shared by the two scripts are in
`scripts/config.sh`.

Settings can be overridden on the command line, e.g. `make start MEMORY_MB=2048`; see the top of the `Makefile`. The
disk is used as it comes in the Alpine image, without resizing.

Once sealed, the image never changes: `bin/BridgeBox` attaches its disk read-only, and the root filesystem of the VM
is an overlay with a tmpfs on top (the `overlaytmpfs` boot option of Alpine), so whatever the VM writes is lost when it
stops. cloud-init is disabled in the sealed image, and the EFI variables are created from scratch at each start in
`/tmp/bridgebox-<uid>/`.

The VM uses the NAT network of Virtualization.framework: it gets an address reachable from the Mac but not from the
local network. The address is found in `/var/db/dhcpd_leases` by the fixed MAC address of the VM.

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


## License

BridgeBox is licensed under the Apache License, Version 2.0: see [LICENSE.md](LICENSE.md).

---
Copyright 2026 Fabrizio Giudici
