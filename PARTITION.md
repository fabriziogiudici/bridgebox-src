## Create the partitions

1. Open **Disk Utility**
2. Identify the SSD and select it.
3. Click **Erase**.
4. Select **GUID Partition Map (GPT)**.
5. After completion, click **Partition** (that was previously greyed out).
6. Click **+** to add a second partition:
    + the former partition must be sized to 250GB;
    + the latter partition must take the remainder of the volume.
7. Press **Partition** to confirm.
8. Identify the volume id and run `diskutil list /dev/diskX`; the result should be something like:
```
/dev/diskN (external, physical):
   #:                       TYPE NAME                    SIZE       IDENTIFIER
   0:      GUID_partition_scheme                        *4.0 TB     diskN
   1:                        EFI EFI                     209.7 MB   diskNs1
   2:                 Apple_APFS Container disk7         250.0 GB   diskNs2
   3:                 Apple_APFS Container disk8         3.7 TB     diskNs3
```
9. Eject the volume.

## Create the ZPool

1. From a Linux box run this command and find the device name:
```shell
lsblk
```

MISSING: CHANGE THE PARTITION TYPE!

2. Create the pool:
```shell
ZPOOL_NAME=Clone2
ZPOOL_OPTIONS="-o ashift=12 -O normalization=formD -O utf8only=on -O compression=lz4 -O autotrim=on -O xattr=sa" # -O encryption=aes-256-gcm -O keyformat=passphrase 
zpool create -f $ZPOOL_OPTIONS $ZPOOL_NAME /dev/XYZ
zpool status
```

3. Create the filesystems:
```shell
zfs create $ZPOOL_NAME/Backgrounds
zfs create $ZPOOL_NAME/Data
zfs create $ZPOOL_NAME/Extra
zfs create $ZPOOL_NAME/LocalData
zfs create $ZPOOL_NAME/Music
zfs create $ZPOOL_NAME/Photos
zfs create $ZPOOL_NAME/TestSets
zfs create $ZPOOL_NAME/Videos
zfs create $ZPOOL_NAME/VMs
zfs create $ZPOOL_NAME/Veracrypt
# zfs create -o encryption=off -o compression=off $ZPOOL_NAME/Veracrypt
```
4. Verify the filesystems:
```shell
zfs list
zfs get compression,encryption,keyformat,casesensitivity,normalization,utf8only $ZPOOL_NAME/*
```
5. Unmount the pool:
```shell
zpool export $ZPOOL_NAME
```
6. Test whether the pool can be imported:
```shell
zpool import
```
7. Import the pool:
```shell
zpool import $ZPOOL_NAME
zfs unmount $ZPOOL_NAME/Veracrypt && rmdir /$ZPOOL_NAME/Veracrypt
zfs load-key $ZPOOL_NAME
zfs mount -a
```


## Alternate setup: LUKS

cryptsetup luksFormat --type luks2 --cipher aes-xts-plain64 --key-size 512 --sector-size 4096 \
--pbkdf argon2id --pbkdf-memory 262144 --label Clone2-luks $DEV


cryptsetup open --allow-discards --persistent $DEV luks-Clone2
ls -l /dev/mapper/luks-Clone2

zpool create -o ashift=12 -O $ZPOOL_OPTIONS (without encryption) -R /srv/bridgebox Clone2 /dev/mapper/luks-Clone2

Then you create datasets as above.


# doas zfs get -r normalization,utf8only Clone2
# 


# ZFS encryption performance in the BridgeBox VM

## Hypothesis

The low throughput of `make speed-test` on the pool `Clone2` (encrypted with `aes-256-gcm`, `lz4` compression) is caused
by ZFS doing AES-GCM in software: OpenZFS on Linux has no accelerated AES/GCM implementation for aarch64.

All the fio runs use the parameters of `speed-test`:

```shell
fio --name=seq_async --directory=<dir> --size=4G --bs=1M --rw=readwrite --rwmixread=50 --direct=1 \
    --ioengine=libaio --iodepth=16 --numjobs=1 --runtime=30 --group_reporting
```

## Steps and results

### 1. Baseline: encrypted pool, 2 vCPUs

```shell
make speed-test
```

- Read 48.5 MiB/s, write 50.9 MiB/s (about 100 MiB/s combined).
- Submission latency (`slat`) about 9.3 ms (read) and 10.8 ms (write) per 1 MiB I/O: the VM handles one block every
  ~10 ms, so the queue depth of 16 doesn't help.
- CPU: `usr=0.10%`, `sys=52.33%`: the time is spent in the kernel, not waiting for the disk.

### 2. OpenZFS sources

In the OpenZFS sources (master, November 2026), the accelerated AES and GCM code of the ICP (the crypto layer of ZFS)
exists only for x86_64 (`module/icp/asm-x86_64/aes`, `module/icp/asm-x86_64/modes`); `module/icp/asm-aarch64` contains
only `blake3` and `sha2`.

### 3. Implementation in use in the VM

```shell
bin/BridgeBox ssh 'cat /sys/module/*/parameters/icp_aes_impl /sys/module/*/parameters/icp_gcm_impl'
```

```
cycle [fastest] generic cycle [fastest] generic
```

Only the generic C implementations are available, for both AES and GCM.

### 4. Unencrypted dataset on the same pool

`Clone2/Veracrypt` has `encryption=off` and `compression=off`:

```shell
bin/BridgeBox ssh -t 'doas mkdir /srv/bridgebox/Clone2/Veracrypt/.t && doas fio --name=seq_async \
  --directory=/srv/bridgebox/Clone2/Veracrypt/.t --size=4G --bs=1M --rw=readwrite --rwmixread=50 --direct=1 \
  --ioengine=libaio --iodepth=16 --numjobs=1 --runtime=30 --group_reporting; doas rm -rf /srv/bridgebox/Clone2/Veracrypt/.t'
```

- Read 311 MiB/s, write 328 MiB/s (about 640 MiB/s combined): 6 times faster; the whole 4 GiB were done in 6.4 s.
- `slat` about 0.36 ms (read) and 2.7 ms (write).
- CPU: `usr=1.36%`, `sys=6.15%`.

The dataset also differs by `compression=off`, but `lz4` is cheap and gives up quickly on incompressible data, so
nearly all the difference is due to encryption.

### 5. Encrypted pool, 4 vCPUs

```shell
make stop && make start CPUS=4 DISKS="..." && make speed-test
```

- Read 64.1 MiB/s, write 67.6 MiB/s (about 132 MiB/s combined): +32%.
- Read `slat` went down from 9.3 to 3.8 ms; write `slat` stayed at about 11.2 ms.
- CPU: `sys=70.61%`.

More vCPUs help reads only partly and writes not at all: each direct write is encrypted on a single core while fio
waits (about 90 MiB/s per core).

### 6. Kernel crypto speed

```shell
bin/BridgeBox ssh 'doas cryptsetup benchmark -c aes-xts-plain64 -s 512'
```

```
# Tests are approximate using memory only (no storage IO).
# Algorithm |       Key |      Encryption |      Decryption
    aes-xts        512b      3875.1 MiB/s      3934.9 MiB/s
```

The Linux kernel crypto, which uses the ARMv8 crypto extensions, runs at about 3.9 GiB/s per core.

## Summary

| Test | Read | Write | Combined | sys CPU |
|---|---|---|---|---|
| `Clone2`, encrypted, 2 vCPUs | 48.5 MiB/s | 50.9 MiB/s | ~100 MiB/s | 52% |
| `Clone2`, encrypted, 4 vCPUs | 64.1 MiB/s | 67.6 MiB/s | ~132 MiB/s | 71% |
| `Clone2/Veracrypt`, unencrypted, 2 vCPUs | 311 MiB/s | 328 MiB/s | ~640 MiB/s | 6% |

The hypothesis is confirmed: ZFS native encryption runs in software on Apple Silicon, and it costs a factor of 5-6;
without it, the limit is the virtio-blk path to the SSD (about 640 MiB/s combined).

## LUKS

Encrypting the partition with LUKS (dm-crypt) and creating the pool on it with `encryption=off` moves the
encryption to the kernel crypto, about 40 times faster than ZFS's generic code (3.9 GiB/s against about 90 MiB/s per
core).

Measured after recreating `Clone2` on LUKS2 (`aes-xts-plain64`, 512-bit key, 4096-byte sectors, Argon2id with
256 MiB), with `ashift=12`, `compression=lz4` and no ZFS encryption, 2 vCPUs, same `make speed-test`:

- Read 228 MiB/s, write 240 MiB/s (about 470 MiB/s combined): 4.7 times the ZFS-encrypted pool, about 73% of the
  unencrypted dataset.
- `slat` about 0.41 ms (read) and 3.8 ms (write).
- CPU: `usr=2.14%`, `sys=24.03%`.

| Test | Read | Write | Combined | sys CPU |
|---|---|---|---|---|
| ZFS encryption (`aes-256-gcm`), 2 vCPUs | 48.5 MiB/s | 50.9 MiB/s | ~100 MiB/s | 52% |
| LUKS + ZFS without encryption, 2 vCPUs | 228 MiB/s | 240 MiB/s | ~470 MiB/s | 24% |
| No encryption, no compression, 2 vCPUs | 311 MiB/s | 328 MiB/s | ~640 MiB/s | 6% |

The remaining gap to the unencrypted dataset is partly dm-crypt and partly `lz4`, which the unencrypted dataset didn't
have.

## Cyclic write slowdowns: TRIM

During long CCC backups to `Clone2` (over SMB), the throughput alternated between about 500 MB/s and about a fifth of
it, for minutes at a time. `zpool iostat -l` showed that in the slow phases the device under ZFS was about 5 times
slower for writes (`disk_wait` 58-60 ms against 11 ms) and for reads too: the cause was below ZFS. Ruled out:

- the source and CCC (a 30 GB folder was copied at full speed throughout);
- the per-file synchronous writes (`zfs set sync=disabled` changed nothing);
- the background freeing of a destroyed dataset (`zpool get freeing` was 0);
- the SSD in general (Blackmagic Disk Speed Test, on the APFS partition of the same SSD: sustained 750-800 MB/s).

The cause was that TRIM never reached the SSD, which didn't know which blocks were free (and so had little room for
its SLC cache): `lsblk -D` showed that virtio-blk supports discards (`DISC-MAX 1.4T` on `vdb`), but the LUKS volume
didn't pass them (`DISC-MAX 0B`, discards are disabled by default), and ZFS had `autotrim=off`. Fixed with:

```shell
doas cryptsetup refresh --allow-discards --persistent bridgebox-<LUKS uuid>   # stored in the LUKS header
doas zpool reopen Clone2         # ZFS checks TRIM support only when it opens a device
doas zpool trim Clone2           # a full TRIM, once
doas zpool set autotrim=on Clone2
```

After the TRIM (and some minutes of idle time), a resync of about 120 GB ran at a steady 385-490 MB/s, with write
`disk_wait` at 7-10 ms throughout. Discards reveal to who analyzes the disk which blocks are free (not their content).

Do not apply this option to Veracrypt devices, also for security reasons.
